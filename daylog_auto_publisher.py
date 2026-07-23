import os
import re
import json
import base64
import requests
import anthropic
from datetime import datetime, date, timedelta, timezone
from korean_lunar_calendar import KoreanLunarCalendar

# .env 파일 로드: 프로젝트 폴더 우선, 없으면 홈 디렉토리 폴백
for _env_path in [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.expanduser("~/.env"),
]:
    if os.path.exists(_env_path):
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
        break

# ==========================================
# 1. 필수 설정 (GitHub Secrets 또는 .env)
# ==========================================
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
DAYLOG_WP_SITE_URL     = os.environ.get("DAYLOG_WP_SITE_URL", "https://daylog.bestwellth.org")
DAYLOG_WP_USERNAME     = os.environ.get("DAYLOG_WP_USERNAME")
DAYLOG_WP_APP_PASSWORD = os.environ.get("DAYLOG_WP_APP_PASSWORD")
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID")

for key, val in [
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ("DAYLOG_WP_USERNAME", DAYLOG_WP_USERNAME),
    ("DAYLOG_WP_APP_PASSWORD", DAYLOG_WP_APP_PASSWORD),
]:
    if not val:
        raise ValueError(f"{key} 환경변수가 설정되지 않았습니다.")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL_NAME = "claude-haiku-4-5"
print(f"Using model: {MODEL_NAME}\n")

KST = timezone(timedelta(hours=9))
CALENDAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_schema.json")
SITE_TARGET = "daylog"

# 카테고리 → 제목 접두어 (H1 태그가 아닌 텍스트 접두어. 본문에는 H1 사용 금지, H2부터 시작)
CATEGORY_PREFIX = {
    "세금·행정마감": "[세금·행정마감]",
    "명절·기념일": "[명절·기념일]",
    "생활·집안일": "[생활·집안일]",
    "여행·쇼핑·이벤트": "[여행·쇼핑·이벤트]",
}

# 카테고리 → 워드프레스 실제 카테고리 분류(taxonomy) ID (wp-admin 글 > 카테고리에서 생성됨)
CATEGORY_WP_ID = {
    "세금·행정마감": 4,
    "명절·기념일": 5,
    "생활·집안일": 6,
    "여행·쇼핑·이벤트": 7,
}

CAT_COLOR        = "#6366f1"
CAT_LIGHT_BG     = "#eef2ff"
CAT_LIGHT_BORDER = "#c7d2fe"
CAT_DARK         = "#4338ca"


# ==========================================
# 캘린더 JSON 읽기/쓰기
# ==========================================
def load_calendar():
    with open(CALENDAR_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_calendar(data):
    with open(CALENDAR_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def today_kst():
    return datetime.now(KST).date()


def solar_date_for_year(item, year):
    """item의 date(MM-DD)를 주어진 year 기준 양력 date 객체로 변환. 음력이면 자동 환산."""
    month, day = (int(x) for x in item["date"].split("-"))
    if item.get("calendar_type") == "lunar":
        cal = KoreanLunarCalendar()
        cal.setLunarDate(year, month, day, False)
        return date(cal.solarYear, cal.solarMonth, cal.solarDay)
    return date(year, month, day)


def find_due_items(data):
    """
    오늘(KST) 기준 [발행일, 이벤트일) 윈도우 안에 있고 해당 연도 미발행인 항목 목록 반환.
    (item, event_year, event_date) 튜플 리스트.

    정확히 발행일 당일에만 매칭하지 않고 윈도우로 판단하는 이유:
    GitHub Actions가 하루 실패/스킵되더라도 다음 실행에서 자동으로 따라잡기 위함.
    단, 이벤트 당일(event_date)은 윈도우에서 제외한다 — 반드시 이벤트가 오기 전에 작성하고,
    윈도우를 통째로 놓쳤다면 당일·이후에 뒤늦게 쓰지 않고 다음 해로 넘긴다.
    """
    today = today_kst()
    due = []
    for item in data.get("items", []):
        if item.get("site_target") != SITE_TARGET:
            continue
        for candidate_year in (today.year - 1, today.year, today.year + 1):
            try:
                event_date = solar_date_for_year(item, candidate_year)
            except Exception as e:
                print(f"[{item['id']}] 날짜 계산 실패: {e}")
                continue
            lead = item.get("lead_time_days", 0)
            publish_date = event_date - timedelta(days=lead)
            if publish_date <= today < event_date:
                if item.get("last_published_year") == event_date.year:
                    print(f"[{item['id']}] {event_date.year}년분 이미 발행됨 — 스킵")
                else:
                    due.append((item, event_date.year, event_date))
                break
    return due


# ==========================================
# WordPress REST API 헬퍼
# ==========================================
def wp_auth_header():
    token = base64.b64encode(f"{DAYLOG_WP_USERNAME}:{DAYLOG_WP_APP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def wp_create_draft(title, content, excerpt, slug, category_id=None):
    """status=draft 로만 저장. 발행(publish) 절대 금지 — 코로님이 수동 검토 후 게시."""
    payload = {
        "title": title, "content": content, "excerpt": excerpt,
        "status": "draft", "slug": slug,
    }
    if category_id:
        payload["categories"] = [category_id]
    resp = requests.post(
        f"{DAYLOG_WP_SITE_URL}/wp-json/wp/v2/posts",
        headers=wp_auth_header(), json=payload, timeout=15,
    )
    if not resp.ok:
        print(f"WordPress API 오류 {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()


def wp_update_rank_math(post_id, focus_keyword, meta_description):
    """
    일반 wp/v2/posts meta 엔드포인트로는 Rank Math 필드가 저장되지 않는다
    (Rank Math가 REST에 show_in_rest로 등록해두지 않음). 대신 Rank Math가
    직접 제공하는 전용 REST 엔드포인트(/rankmath/v1/updateMeta)를 사용한다.
    """
    payload = {
        "objectType": "post",
        "objectID": post_id,
        "meta": {
            "rank_math_focus_keyword": focus_keyword,
            "rank_math_description": meta_description,
        },
    }
    resp = requests.post(
        f"{DAYLOG_WP_SITE_URL}/wp-json/rankmath/v1/updateMeta",
        headers=wp_auth_header(), json=payload, timeout=15,
    )
    if resp.status_code == 200:
        print(f"Rank Math 메타 업데이트 완료 (post_id: {post_id})")
    else:
        print(f"Rank Math 메타 업데이트 실패: {resp.status_code} {resp.text[:200]}")


# ==========================================
# 텔레그램 전송
# ==========================================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        print("텔레그램 전송 완료" if resp.status_code == 200 else f"텔레그램 전송 실패: {resp.text}")
    except Exception as e:
        print(f"텔레그램 전송 오류: {e}")


# ==========================================
# HTML 구조 가이드 (4탭 대시보드 — 워드프레스 탭용 본문 구조)
# ==========================================
def build_daylog_html_guide():
    return f"""
[HTML 구조 — 반드시 이 순서, 이 스타일 그대로. 절대 생략 금지]

[모바일 필수 규칙]
- 본문 최소 font-size:15px, 표 내부 최소 font-size:13px, 캡션·출처만 font-size:12px 허용
- 모든 표는 반드시 overflow-x:auto + word-break:keep-all 적용
- 표 컬럼 최대 3개 — 4개 이상 필요 시 카드형으로 전환

--- 1. 카테고리 뱃지 ---
<div style="display:inline-block;background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:700;padding:4px 14px;border-radius:20px;margin-bottom:14px;">[카테고리명] · [서브라벨]</div>

--- 2. 서브 제목 (H1 절대 금지 — 본문은 H2부터 시작) ---
<div style="font-size:clamp(22px,4vw,28px);font-weight:800;color:#1e293b;margin:0 0 8px 0;line-height:1.4;">[서브 문구]</div>

--- 3. 핵심 정보 요약 박스 ---
<div style="background:#f8fafc;padding:28px 30px;border-radius:16px;border:1px solid #e2e8f0;margin-bottom:40px;">
  <p style="margin-top:0;font-size:13px;font-weight:700;color:#94a3b8;letter-spacing:0.08em;margin-bottom:16px;">한눈에 보기</p>
  <ul style="list-style:none !important;padding:0 !important;margin:0 0 24px 0 !important;">
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;margin-bottom:10px;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[핵심 정보 1]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;margin-bottom:10px;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[핵심 정보 2]</span></li>
    <li style="display:flex;align-items:flex-start;gap:12px;font-size:15px;color:#334155;line-height:1.8;list-style:none;"><span style="display:inline-block;width:6px;height:6px;min-width:6px;background:{CAT_COLOR};border-radius:50%;margin-top:9px;flex-shrink:0;"></span><span style="flex:1;">[핵심 정보 3]</span></li>
  </ul>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:0 0 20px 0;">
  <div style="display:flex;flex-wrap:wrap;gap:8px;">
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드1]</span>
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드2]</span>
    <span style="background:{CAT_LIGHT_BG};color:{CAT_COLOR};font-size:13px;font-weight:600;padding:4px 12px;border-radius:20px;">#[키워드3]</span>
  </div>
</div>

--- 3-1. 목차 (요약 박스 바로 아래 — 각 H2 섹션으로 점프하는 앵커 링크. Rank Math 콘텐츠 가독성 체크 대응) ---
각 H2에는 고유 id를 붙이고(예: id="section-1"), 목차 링크가 그 id로 이동하게 한다.
<div style="background:#f8fafc;border-radius:12px;padding:20px 24px;margin-bottom:32px;">
  <p style="margin:0 0 12px 0;font-size:13px;font-weight:800;color:#94a3b8;letter-spacing:0.05em;">목차</p>
  <ul style="list-style:none;padding:0;margin:0;">
    <li style="margin-bottom:8px;"><a href="#section-1" style="font-size:14px;color:{CAT_COLOR} !important;text-decoration:none;font-weight:600;">1. [섹션1 제목]</a></li>
    <li style="margin-bottom:8px;"><a href="#section-2" style="font-size:14px;color:{CAT_COLOR} !important;text-decoration:none;font-weight:600;">2. [섹션2 제목]</a></li>
    <li><a href="#section-3" style="font-size:14px;color:{CAT_COLOR} !important;text-decoration:none;font-weight:600;">3. [섹션3 제목]</a></li>
  </ul>
</div>

--- 3-3. 애드센스 슬롯 A — 디스플레이 광고 (목차 바로 아래, 본문 시작 전 — 아래 코드를 정확히 그대로 복사) ---
※ daylog 애드센스 승인 완료. 아래는 실제 광고 코드이므로 값을 절대 임의로 바꾸지 않는다. 클릭을 유도하는 문구는 절대 추가하지 않는다.
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766" crossorigin="anonymous"></script>
<!-- 디스플레이광고 -->
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-6858780475640766" data-ad-slot="1825484842" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>

--- 4. 본문 섹션 3개 (절대 생략 금지, H2부터 시작, 각 h2에 id="section-N" 부여) ---
각 섹션은 아래 구조를 따른다 (id="section-N"은 목차 앵커와 반드시 일치시킬 것):
<div id="section-N" style="margin-bottom:56px;padding-top:40px;border-top:1px solid #e2e8f0;">
  <h2 style="font-size:clamp(18px,3vw,22px);font-weight:800;color:#1e293b;margin:0 0 8px 0;line-height:1.4;">[제목 — 최소 1개 섹션의 H2 제목에는 포커스 키워드를 그대로 포함할 것]</h2>
  <p style="font-size:15px;color:#94a3b8;font-weight:600;margin:0 0 20px 0;">[서브 문구]</p>
  <p style="font-size:15px;color:#334155;line-height:1.9;margin-bottom:16px;">[핵심 내용 서술 — 섹션당 최소 3~4문장 이상으로 충분히 설명할 것. 문단 안에서 가장 중요한 핵심 문장(또는 구절) 1개는 반드시 <mark style="background:#fef08a;padding:0 2px;border-radius:2px;">이렇게</mark> 형광펜 강조 태그로 감싼다]</p>
  [내용 성격에 맞는 컴포넌트 1개 이상 — 표/글머리기호 박스/스텝 박스/주의사항 박스 중 선택]
</div>

--- 4-0. 형광펜 강조 규칙 (본문이 길어 훑어보기 어려우므로 반드시 지킬 것) ---
- 각 섹션(본문 3개)마다 그 섹션에서 독자가 가장 먼저 눈에 담아야 할 핵심 문장 또는 핵심 구절을 1개(최대 2개)씩 골라 <mark style="background:#fef08a;padding:0 2px;border-radius:2px;">문장</mark>으로 감싼다.
- 문장 전체를 통째로 감싸지 말고, 그 문장에서도 정말 핵심적인 구절(날짜, 수치, 결론 등)만 짧게 감싼다 — 과하게 넓은 범위를 감싸거나 문단마다 남발하면 형광펜 효과가 사라진다.
- 표·박스 컴포넌트 안의 텍스트에는 사용하지 않는다 (이미 시각적으로 강조되어 있음). 본문 문단(<p>)에만 사용한다.

--- 4-1. 애드센스 슬롯 B — 가로형 인피드 (섹션 1 바로 아래, 섹션 2 시작 전 — 아래 코드를 정확히 그대로 복사) ---
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766" crossorigin="anonymous"></script>
<!-- 가로형 인피드 -->
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-6858780475640766" data-ad-slot="5165217836" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>

--- 4-2. 애드센스 슬롯 C — 인아티클 플루이드 (섹션 2 바로 아래, 섹션 3 시작 전 — 아래 코드를 정확히 그대로 복사) ---
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766" crossorigin="anonymous"></script>
<ins class="adsbygoogle" style="display:block; text-align:center;" data-ad-layout="in-article" data-ad-format="fluid" data-ad-client="ca-pub-6858780475640766" data-ad-slot="8908458700"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>

▶ 항목 나열·체크리스트 → 글머리기호 박스
<div style="background:#f8fafc;border-radius:12px;padding:20px 24px;margin:16px 0;">
  <ul style="list-style:none;padding:0;margin:0;">
    <li style="display:flex;align-items:flex-start;gap:10px;font-size:15px;color:#334155;line-height:1.8;margin-bottom:8px;"><span style="color:{CAT_COLOR};font-weight:800;flex-shrink:0;">✓</span><span>[항목 내용]</span></li>
  </ul>
</div>

▶ 날짜·조건 비교 → 최대 3컬럼 표 (th는 항상 center, 단문 15자 이하 center, 장문 left)
<div style="overflow-x:auto;margin:16px 0;word-break:keep-all;">
  <table style="width:100%;border-collapse:collapse;font-size:13px;">
    <thead><tr style="background:{CAT_COLOR};color:#fff;">
      <th style="padding:11px 14px;text-align:center;font-weight:700;">[구분]</th>
      <th style="padding:11px 14px;text-align:center;font-weight:700;">[항목A]</th>
      <th style="padding:11px 14px;text-align:center;font-weight:700;">[항목B]</th>
    </tr></thead>
    <tbody>
      <tr style="border-bottom:1px solid #e2e8f0;"><td style="padding:11px 14px;text-align:center;color:#334155;">[행 라벨]</td><td style="padding:11px 14px;text-align:center;color:#334155;">[값A]</td><td style="padding:11px 14px;text-align:left;color:#334155;">[장문 값B]</td></tr>
    </tbody>
  </table>
</div>

▶ 절차·타임라인 → 스텝 박스
<div style="margin:16px 0;">
  <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:12px;">
    <div><span style="background:{CAT_COLOR};color:#fff;font-size:13px;font-weight:800;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;">1</span></div>
    <div style="padding-top:4px;"><p style="margin:0 0 4px 0;font-size:15px;font-weight:700;color:#1e293b;">[단계명]</p><p style="margin:0;font-size:15px;color:#334155;line-height:1.7;">[단계 설명]</p></div>
  </div>
</div>

▶ 주의·마감 임박 → 주의사항 박스
<div style="background:#fef9c3;border-left:4px solid #eab308;border-radius:0 12px 12px 0;padding:16px 20px;margin:16px 0;">
  <p style="margin:0 0 8px 0;font-size:13px;font-weight:800;color:#854d0e;">⚠ 주의사항</p>
  <ul style="list-style:none;padding:0;margin:0;">
    <li style="font-size:15px;color:#334155;line-height:1.8;margin-bottom:6px;">· [주의 항목]</li>
  </ul>
</div>

▶ 참고자료에 실제 공식 사이트가 있는 주제(정부·공공기관 서비스 등) → 공식 사이트 바로가기 CTA
※ 참고자료 섹션에 이미 적은 것과 동일한 URL만 사용한다 — 새로운 URL을 지어내지 않는다.
<div style="text-align:center;margin:28px 0;"><div style="display:inline-block;background:{CAT_COLOR};border-radius:8px;padding:13px 30px;"><a href="[참고자료의 실제 URL]" target="_blank" rel="noopener noreferrer" style="color:#fff !important;font-size:15px;font-weight:700;text-decoration:none;">[사이트명]에서 확인하기</a></div></div>

▶ 상품·숙소 추천이 실제로 자연스러운 주제(계절가전·생활용품·여행숙소 등) → 제휴 CTA 카드
※ 이 컴포넌트는 광고 클릭을 유도하는 문구가 아니라, 본문에서 실제로 추천한 상품/숙소를 보러 가는 정상적인 제휴 링크다.
※ 실제 URL은 알 수 없으므로 절대 임의로 만들어내지 말고 아래 placeholder를 그대로 사용한다 — 코로님이 발행 전 실제 제휴 링크로 교체한다.
<div style="background:{CAT_LIGHT_BG};border:1px solid {CAT_LIGHT_BORDER};border-radius:14px;padding:20px 22px;margin:20px 0;">
  <p style="margin:0 0 6px 0;font-size:13px;font-weight:800;color:{CAT_DARK};">🛒 [추천 상품/숙소명]</p>
  <p style="margin:0 0 14px 0;font-size:14px;color:#334155;line-height:1.7;">[이 글 맥락에서 실제로 도움되는 이유 1~2줄]</p>
  <a href="{{COUPANG_AFFILIATE_LINK}}" target="_blank" rel="noopener noreferrer nofollow sponsored" style="display:inline-block;background:{CAT_COLOR};color:#fff !important;font-size:14px;font-weight:700;padding:10px 20px;border-radius:8px;text-decoration:none;">쿠팡에서 확인하기</a>
</div>
(여행·숙박이 주제인 글은 위 카드를 숙박 버전으로: href="{{ACCOMMODATION_AFFILIATE_LINK}}", 버튼 텍스트 "숙소 예약하러 가기")
※ 이 카드는 주제와 무관하게 매 글에 넣지 말고, 실제로 상품/숙소 추천이 자연스러운 글에서만 1개 사용한다.

--- 5. 참고자료 (있는 경우) + 내부 링크 1개 (Rank Math 내부링크 체크 대응) ---
※ 내부 링크는 daylog.bestwellth.org 홈으로 최소 1개 넣는다 (사이트 초기라 다른 글이 적으므로 홈 링크로 충분). 새 URL을 지어내지 말 것 — 아래 홈 링크만 사용.
<div style="margin-top:48px;padding:24px;background:#f8fafc;border-radius:12px;border:1px solid #e2e8f0;"><h4 style="margin:0 0 14px 0;color:#334155;font-size:16px;font-weight:700;">참고 자료</h4><ul style="list-style:none;padding:0;margin:0;font-size:14px;color:#334155;line-height:2.2;"><li>[출처명 — 원문에 근거가 있을 때만 작성, 없으면 이 줄만 생략]</li><li><a href="https://daylog.bestwellth.org/" style="color:{CAT_COLOR} !important;text-decoration:none;">daylog에서 반복일정 콘텐츠 더 보기</a></li></ul></div>

--- 6. 면책조항 ---
<p style="margin-top:2em;font-size:12px;color:#94a3b8;">본 콘텐츠는 정보 제공 목적으로 작성되었습니다. 정확한 일정·기준은 관련 기관의 공식 공고를 반드시 확인하시기 바랍니다.</p>

--- 7. 애드센스 슬롯 D — 오토릴렉스드 (면책조항 바로 아래, 글의 맨 마지막 — 아래 코드를 정확히 그대로 복사) ---
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6858780475640766" crossorigin="anonymous"></script>
<ins class="adsbygoogle" style="display:block" data-ad-format="autorelaxed" data-ad-client="ca-pub-6858780475640766" data-ad-slot="3873632172"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
"""


# ==========================================
# Claude 호출
# ==========================================
def call_claude(prompt):
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=MODEL_NAME,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            print(f"Claude 호출 실패 ({attempt + 1}/3): {e}")
            if attempt < 2:
                import time
                time.sleep(10)
            else:
                raise


def verify_event_facts(item, event_year):
    """
    본문을 쓰기 전 날짜·사실 정확성을 자체 점검하는 별도 호출.
    특히 is_variable(근사 날짜) 항목에서 모델이 확신 없는 날짜를 단정해서 쓰지 않도록,
    작성 전에 스스로 확신 수준을 평가하게 한다.
    """
    calendar_label = "음력" if item.get("calendar_type") == "lunar" else "양력"
    prompt = f"""당신은 daylog.bestwellth.org 팩트체크 담당자입니다.
아래 반복 이벤트 기사를 쓰기 전에, 알고 있는 지식 범위 내에서 날짜·핵심 사실의 정확성을 스스로 점검하세요.
이 단계는 본문 작성이 아니라 사전 검증입니다.

[이벤트]
- 이름: {item['title']}
- 등록된 날짜: {calendar_label} {item['date']} ({event_year}년 기준)
- 근사치 여부(is_variable): {item.get('is_variable')}
- 등록된 메모: {item.get('note') or '(없음)'}

[점검할 것]
1. 이 날짜(또는 날짜 패턴)가 실제 사실과 맞는가?
2. {event_year}년 기준 정확한 날짜를 확신하는가, 아니면 근사치로만 알고 있는가?
3. 본문에서 특정 수치·날짜를 단정하면 안 되는 지점이 있다면 무엇인가?

[응답 형식 — 이 태그만 사용, 다른 설명 금지]
[CONFIDENCE]high|medium|low[/CONFIDENCE]
[VERIFICATION_NOTE]본문 작성 시 반영할 점검 결과를 2~3문장으로[/VERIFICATION_NOTE]"""
    return call_claude(prompt)


def generate_draft(item, event_year, verification_note="", confidence="medium"):
    prefix = CATEGORY_PREFIX.get(item["category"], "")
    title_hint = item["title_template"].format(year=event_year)
    variable_note = (
        f"\n[주의] 이 항목의 날짜는 확정값이 아닌 근사치입니다 ({item.get('note', '')}). "
        "본문에서 특정 일자를 단정하지 말고 '매년 ○월경', '공식 발표 확인 필요' 등으로 서술하세요."
        if item.get("is_variable") else ""
    )
    fact_check_note = (
        f"\n[사전 팩트체크 결과 — 반드시 반영]\n확신 수준: {confidence}\n{verification_note}\n"
        + ("확신 수준이 낮으므로 구체적 날짜·수치를 단정하지 말고 '공식 공고 확인 필요' 표현을 적극 사용하세요.\n"
           if confidence == "low" else "")
        if verification_note else ""
    )

    prompt = f"""당신은 daylog.bestwellth.org 반복일정팀 전문 에디터입니다.
매년 반복되는 생활 이벤트를 실제 독자가 검색하는 시점보다 먼저 콘텐츠화하여 조회수를 선점하는 것이 목적입니다.

[이벤트 정보]
- 이벤트명: {item['title']}
- 카테고리: {item['category']} (제목에 반드시 "{prefix}" 접두어 사용)
- 타겟 독자층: {item['audience']}
- 대상 연도: {event_year}년
- 제목 힌트: {title_hint}{variable_note}
{fact_check_note}
[네이버블로그 ↔ 워드프레스 관계 — 반드시 이 순서로 사고할 것]
같은 내용을 요약·재서술해서 두 채널에 중복 게재하지 않는다. 대신 역할을 분리한다.
1. 먼저 네이버블로그 글을 "이 이벤트의 전체 개요"로 구상한다 — 언제·누가 대상·무엇을·어떻게 하면 되는지, 순수 텍스트 위주로 폭넓게 다룬다. (예: "재산세 납부기간" 같은 넓은 키워드를 노림)
2. 그 다음 워드프레스 글은 네이버 글이 다루는 내용 중 **깊이 있게 파고들 가치가 있는 한 부분**(계산 방식, 조건별 차이, 자격 기준 등 구체적 수치·표가 필요한 부분)을 골라 그것만 집중적으로 확장한 파생 글로 작성한다. (예: "재산세 계산법" 같은 좁고 구체적인 키워드를 노림) 네이버 글 전체를 다시 요약하지 않는다.
3. 두 글이 서로 연결된 시리즈처럼 느껴지도록 자연스러운 연결 문구를 넣는다 — 네이버 글 끝에는 "~부분은 별도로 자세히 다룰 예정입니다" 같은 문장을, 워드프레스 글 도입부에는 "~에 이어 이번에는 ~를 자세히 살펴봅니다" 같은 문장을 자연스럽게 포함한다.
4. 워드프레스 글에서 다루는 심화 파트(계산법 등)는 매년 바뀌는 세율·비율 같은 구체적 수치를 단정하지 말고, 안정적으로 알려진 구조(공식, 산정 방식, 표준 세율표처럼 자주 바뀌지 않는 부분)를 중심으로 설명한다. 연도별로 조정되는 값은 "공식 사이트에서 확인" 처리한다.

[작성 원칙]
- 제목은 "{prefix}"로 시작, 힌트를 참고하되 자연스럽게 다듬어도 됨
- H1 태그 절대 사용 금지, 본문은 H2부터 시작
- 문어체(이다/한다/했다), 구어체·이모티콘 금지 (단, 가이드에 명시된 이모지 아이콘은 허용)
- HTML을 ```html 코드블록으로 감싸지 말 것 — 순수 HTML만 출력
- 확인되지 않은 수치·날짜를 단정하지 말 것 — 근거 없으면 "공식 공고 확인 필요"로 표기
- 표는 컬럼 최대 3개, 단문(15자 이하) 셀은 center, 장문 셀은 left, th는 항상 center
- 글 반드시 끝까지 완성
- "클릭해주세요", "광고를 눌러주세요" 같은 광고·링크 클릭 유도 문구 절대 금지 (구글 애드센스 정책 위반 — 계정 정지 사유)
- 제휴 CTA 카드는 본문 주제와 실제로 관련된 상품·숙소를 추천할 때만 1개 사용, 억지로 넣지 말 것
- 공식 사이트 CTA 버튼은 참고자료에 실제 출처가 있을 때만, 그 URL을 그대로 재사용해서 1개 사용 가능

[Rank Math SEO 체크리스트 준수 — 반드시 지킬 것. 포커스 키워드는 코로님이 편집기에서 직접 입력하므로, 아래 항목이 실제로 준수되어 있어야 입력 즉시 체크가 통과함]
- SEO 제목([TITLE] 태그, "{prefix}" 접두어 다음)의 앞부분에 포커스 키워드를 그대로 포함
- 메타 설명([META_DESC])에 포커스 키워드를 그대로 포함
- 슬러그([SLUG])에 포커스 키워드의 영문 표현을 포함
- 본문 첫 문단에 포커스 키워드를 그대로 포함
- 본문 전체에서 포커스 키워드(또는 자연스러운 변형)를 여러 번 반복 — 전체 분량의 약 1% 밀도를 목표
- 본문 섹션 3개 중 최소 1개의 H2 제목에 포커스 키워드를 그대로 포함
- 대표 이미지 alt 텍스트에 포커스 키워드 포함 (가이드의 이미지 placeholder 참고)
- 본문 텍스트 분량은 최소 600단어 이상 (한글 기준 공백 포함 약 1,500자 이상) — 섹션당 서술을 충분히 길게 쓸 것, 짧게 요약하지 말 것
- 목차(가이드 3-1)와 내부 링크(가이드 5, daylog 홈)를 반드시 포함
- 외부 참고자료 링크를 최소 1개 포함 (가이드 5)

{build_daylog_html_guide()}

[SEO정보 탭 — 반드시 출력]
[FOCUS_KW]3~4단어 롱테일 키워드 — 워드프레스 글(심화 파트)에 맞는 키워드[/FOCUS_KW]
[META_DESC]130~155자 자연스러운 문장형 메타 설명 — 해시태그·버튼텍스트 금지[/META_DESC]
[SLUG]영문 슬러그[/SLUG]
[EXCERPT]100~150자 발췌문[/EXCERPT]
[HASHTAGS]검색·공유용 해시태그 5개, "#태그1 #태그2 #태그3 #태그4 #태그5" 형식, 본문에는 넣지 않고 이 태그로만 출력[/HASHTAGS]
[이미지 프롬프트 공통 규칙 — 아래 IMAGE_PROMPT_* 태그 전부에 적용]
독자 대부분이 국내(한국) 이용자이므로, 이미지 안에 렌더링될 모든 텍스트(제목·부제·라벨·아이콘 옆 문구 등)는 브랜드명 등 고유어를 제외하면 **거의 전부 한글로 작성**해야 한다는 지시를 프롬프트 문장에 명시적으로 포함한다. "VAT", "Tax", "Deadline" 같은 영문 단어가 이미지에 렌더링되지 않도록 "이미지 내 텍스트는 고유명사를 제외하고 모두 한글로 표기, 영문 사용 금지"라는 취지의 문장을 프롬프트 끝부분에 반드시 넣는다.
[IMAGE_PROMPT_NAVER_1_1]네이버 카드뉴스용 1:1 정사각형 이미지 프롬프트, 한글로 작성. **아래 NAVER_TITLE 태그에 쓸 문구(넓은 개요 키워드 기준)를 대제목으로 사용한다 — TITLE(워드프레스 심화 제목)을 재사용하지 않는다.** 아이콘·색상 블록 위주의 추상적 인포그래픽이 아니라, 이 글의 주제를 직관적으로 알아볼 수 있는 **실사 사진 또는 고퀄리티 애니메이션(일러스트) 스타일** 장면을 중앙에 크게 배치하도록 묘사한다(예: 관련 인물·사물·장소를 담은 사진풍 또는 일러스트풍 장면). 그 배경 위에 반투명 단색 컬러 오버레이를 입혀 글자가 잘 보이게 하고, 화면 중앙에 NAVER_TITLE 기반 대제목과 그에 어울리는 부제 텍스트를 배치하되 **둘 사이의 위계를 명확히 하도록 지시한다** — 대제목은 가장 크고 굵은 글씨로 화면에서 가장 먼저 눈에 띄게, 부제는 대제목보다 확연히 작은 글씨 크기(대략 대제목의 절반 수준)와 얇은 굵기로 대제목 아래 보조적으로 배치한다고 명시한다. 실제로 들어갈 제목·부제 문구를 프롬프트에 그대로 포함해 이미지 생성기가 렌더링하도록 요청한다(텍스트 정확도 제약 없음). 이미지 내 모든 텍스트는 고유명사를 제외하고 한글로 표기하며 영문 사용을 금지한다는 문장을 포함한다. 워터마크·로고·특정 브랜드/실존 인물 식별 요소는 넣지 않는다[/IMAGE_PROMPT_NAVER_1_1]
[IMAGE_PROMPT_WP_16_9]워드프레스 대표이미지용 16:9 가로형 이미지 프롬프트, 한글로 작성. **위 TITLE 태그에 쓸 문구(포커스 키워드가 들어간 심화 제목)를 대제목으로 사용한다 — NAVER_TITLE과는 다른 문구여야 한다.** 위와 동일하게 추상적 인포그래픽이 아니라 **실사 사진 또는 애니메이션(일러스트) 스타일**의 주제를 직관적으로 보여주는 장면을 화면 중앙에 크게 배치. 배경에 반투명 단색 컬러 오버레이를 입혀 가독성을 확보하고, 중앙에 TITLE 기반 대제목과 그에 어울리는 부제 텍스트를 배치하되 **둘 사이의 위계를 명확히 하도록 지시한다** — 대제목은 가장 크고 굵은 글씨로 화면에서 가장 먼저 눈에 띄게, 부제는 대제목보다 확연히 작은 글씨 크기(대략 대제목의 절반 수준)와 얇은 굵기로 대제목 아래 보조적으로 배치한다고 명시하며, 실제 문구를 프롬프트에 그대로 포함한다(텍스트 정확도 제약 없음). 이미지 내 모든 텍스트는 고유명사를 제외하고 한글로 표기하며 영문 사용을 금지한다는 문장을 포함한다. 워터마크·로고·특정 브랜드/실존 인물 식별 요소는 넣지 않는다[/IMAGE_PROMPT_WP_16_9]

[네이버블로그 탭 — 반드시 출력, 전체 개요. 반드시 서론-본론-결론 구조로 작성]
[NAVER_TITLE]네이버블로그용 제목 — 워드프레스 [TITLE]과 반드시 다른 문구로 작성한다. 워드프레스 제목이 심화 파트의 좁고 구체적인 키워드(예: "부가가치세 계산법")를 노린다면, 네이버 제목은 이 이벤트의 넓은 개요 키워드(예: "부가가치세 확정신고 기간과 대상")를 노린다. 카테고리 접두어 "{prefix}"는 붙이지 않는다(네이버는 워드프레스와 별개 채널이라 워드프레스 카테고리 체계를 노출할 필요 없음)[/NAVER_TITLE]
[NAVER_SUMMARY]900~1200자 분량, 순수 텍스트 위주(HTML 태그 없이), 격식 있는 경어체(합니다/습니다 체)로 작성한 이벤트 전체 개요(총론). 워드프레스 심화 파트(구체적 계산·조건·수치)와 겹치지 않게, 같은 주제를 다른 내용 — 언제·대상·무엇을·어떻게 중심 — 으로 작성. 반드시 아래 3단 구조를 지킨다:
- 서론: 이 글의 주제를 소개하고, 오늘 어떤 내용을 정리해줄지 예고하면서 "구체적인 [워드프레스 심화 주제]는 블로그에서 이어서 다룬다"는 취지의 문장을 자연스럽게 포함해 워드프레스와 연동되게 한다.
- 본론: 소주제 2~3개로 구성한다. 각 소주제는 "①/②/③" 같은 기호와 소주제 제목을 한 줄로 적고, 그 아래 본문 내용을 문단으로 서술한다(소주제 제목과 본문 내용을 명확히 구분).
- 결론: 본론 내용을 한 문단으로 요약하고, 워드프레스 심화 글로 자연스럽게 연결되는 CTA 문장을 넣은 뒤 마지막 줄에 "▶ [워드프레스 글 링크]"를 그대로 출력한다(실제 URL은 코로님이 발행 후 직접 채워 넣는다는 전제이므로 URL을 지어내지 않는다).[/NAVER_SUMMARY]

[네이버 소주제별 인포그래픽 이미지 프롬프트 — SEO정보 탭에 표시. NAVER_SUMMARY 본론의 소주제 개수와 정확히 같은 개수만큼 출력할 것]
소주제마다 [IMAGE_PROMPT_SUBTOPIC_1]...[/IMAGE_PROMPT_SUBTOPIC_1], [IMAGE_PROMPT_SUBTOPIC_2]...[/IMAGE_PROMPT_SUBTOPIC_2] 형식으로 번호를 소주제 순서(①②③)와 맞춰 하나씩 출력한다. 이 프롬프트들은 위 대표 이미지 프롬프트(IMAGE_PROMPT_NAVER_1_1 / WP_16_9)와 스타일이 다르다 — 실사·애니메이션이 아니라 **인포그래픽 방식**(아이콘, 색상 블록, 체크리스트, 막대·화살표 등 데이터 시각화, 짧은 텍스트 라벨)으로 그 소주제 본문 내용의 핵심 정보(날짜·대상·체크리스트·수치 등)를 반영해서 작성한다. **16:9 가로형**(1:1 정사각형 아님), 한글로 작성, 실제 라벨 문구를 그대로 포함해 렌더링을 요청한다(텍스트 정확도 제약 없음). 위 [이미지 프롬프트 공통 규칙]과 동일하게 이미지 내 모든 텍스트는 고유명사를 제외하고 한글로 표기하며 영문 사용을 금지한다는 문장을 포함하고, 워터마크·로고·특정 브랜드/실존 인물 식별 요소는 넣지 않는다.

[응답 형식]
[TITLE]{prefix} 제목[/TITLE]
본문 HTML (워드프레스용, 네이버 개요의 심화 파생 글)
[FOCUS_KW]...[/FOCUS_KW][META_DESC]...[/META_DESC][SLUG]...[/SLUG][EXCERPT]...[/EXCERPT][HASHTAGS]...[/HASHTAGS][IMAGE_PROMPT_NAVER_1_1]...[/IMAGE_PROMPT_NAVER_1_1][IMAGE_PROMPT_WP_16_9]...[/IMAGE_PROMPT_WP_16_9]
[NAVER_TITLE]...[/NAVER_TITLE]
[NAVER_SUMMARY]...[/NAVER_SUMMARY]
[IMAGE_PROMPT_SUBTOPIC_1]...[/IMAGE_PROMPT_SUBTOPIC_1][IMAGE_PROMPT_SUBTOPIC_2]...[/IMAGE_PROMPT_SUBTOPIC_2](소주제 개수만큼 반복)"""

    return call_claude(prompt)


# ==========================================
# 파싱 + 발행(draft 저장)
# ==========================================
def parse_and_save_draft(raw, item, event_year, confidence="medium"):
    def extract(tag, default=""):
        m = re.search(rf'\[{tag}\](.*?)\[/{tag}\]', raw, re.DOTALL)
        return m.group(1).strip() if m else default

    title           = extract("TITLE", f"{CATEGORY_PREFIX.get(item['category'], '')} {item['title']}")
    focus_kw        = extract("FOCUS_KW", "")
    meta_desc       = extract("META_DESC", "")
    slug            = extract("SLUG", item["id"])
    excerpt         = extract("EXCERPT", "")
    hashtags        = extract("HASHTAGS", "")
    image_naver_1_1 = extract("IMAGE_PROMPT_NAVER_1_1", "")
    image_wp_16_9   = extract("IMAGE_PROMPT_WP_16_9", "")
    naver_title     = extract("NAVER_TITLE", "")
    naver_summary   = extract("NAVER_SUMMARY", "")

    # 네이버 소주제별 인포그래픽 이미지 프롬프트 — 소주제 개수가 가변적이라 동적으로 추출
    subtopic_images = [
        m.group(2).strip()
        for m in re.finditer(r'\[IMAGE_PROMPT_SUBTOPIC_(\d+)\](.*?)\[/IMAGE_PROMPT_SUBTOPIC_\1\]', raw, re.DOTALL)
    ]

    # 슬러그 안전장치 — 한글 포함 시 영문 슬러그로 대체
    import unicodedata
    def is_korean(c):
        return unicodedata.category(c) == "Lo" and ord(c) >= 0xAC00
    if any(is_korean(c) for c in slug) or len(slug) < 3:
        slug = f"{item['id']}-{event_year}"

    body = raw
    for tag in ["TITLE", "FOCUS_KW", "META_DESC", "SLUG", "EXCERPT", "HASHTAGS",
                "IMAGE_PROMPT_NAVER_1_1", "IMAGE_PROMPT_WP_16_9", "NAVER_TITLE", "NAVER_SUMMARY"]:
        body = re.sub(rf'\[{tag}\].*?\[/{tag}\]\n?', '', body, flags=re.DOTALL)
    body = re.sub(r'\[IMAGE_PROMPT_SUBTOPIC_\d+\].*?\[/IMAGE_PROMPT_SUBTOPIC_\d+\]\n?', '', body, flags=re.DOTALL)
    body = body.strip()
    body = re.sub(r'^```[a-zA-Z]*\n?', '', body, flags=re.MULTILINE)
    body = re.sub(r'\n?```\s*$', '', body, flags=re.MULTILINE)
    body = body.strip()

    category_id = CATEGORY_WP_ID.get(item["category"])
    result = wp_create_draft(title=title, content=body, excerpt=excerpt, slug=slug, category_id=category_id)
    if not result:
        print(f"[{item['id']}] 임시글 저장 실패 — 건너뜀")
        return False

    post_id = result.get("id", "")
    edit_url = f"{DAYLOG_WP_SITE_URL}/wp-admin/post.php?post={post_id}&action=edit"
    print(f"[{item['id']}] 임시글 저장 완료! ID: {post_id}")

    if post_id and focus_kw:
        wp_update_rank_math(post_id, focus_kw, meta_desc)

    confidence_warning = (
        f"\n⚠ 날짜·사실 확신 수준: {confidence.upper()} — 발행 전 직접 확인 권장\n"
        if confidence != "high" else ""
    )
    subtopic_image_lines = "".join(
        f"이미지 프롬프트(네이버 소주제 {i+1}, 인포그래픽): {p}\n"
        for i, p in enumerate(subtopic_images)
    )
    send_telegram(
        f"<b>daylog.bestwellth.org 임시글 저장됨</b>\n\n"
        f"카테고리: {item['category']} · 대상: {item['audience']}\n"
        f"제목(워드프레스): {title}\n"
        f"제목(네이버): {naver_title}\n"
        f"포커스 키워드: {focus_kw}\n"
        f"해시태그: {hashtags}\n"
        f"이미지 프롬프트(네이버 1:1): {image_naver_1_1}\n"
        f"이미지 프롬프트(워드프레스 16:9): {image_wp_16_9}\n"
        f"{subtopic_image_lines}"
        f"{confidence_warning}\n"
        f"편집(검토 후 발행): {edit_url}\n\n"
        f"— 네이버 블로그용 개요 —\n{naver_summary}"
    )
    return True


# ==========================================
# 메인 실행
# ==========================================
def run():
    print("daylog.bestwellth.org 반복일정 자동 초안 생성 시작...\n")
    data = load_calendar()
    due_items = find_due_items(data)

    if not due_items:
        print("오늘 리드타임 도달 항목 없음 — 종료")
        return

    print(f"리드타임 도달 항목 {len(due_items)}건 발견\n")
    updated = False
    for item, event_year, event_date in due_items:
        print(f"--- [{item['id']}] {item['title']} (대상 연도: {event_year}, 이벤트일: {event_date}) ---")
        try:
            verify_raw = verify_event_facts(item, event_year)
            confidence = re.search(r'\[CONFIDENCE\](.*?)\[/CONFIDENCE\]', verify_raw, re.DOTALL)
            confidence = confidence.group(1).strip().lower() if confidence else "medium"
            note_m = re.search(r'\[VERIFICATION_NOTE\](.*?)\[/VERIFICATION_NOTE\]', verify_raw, re.DOTALL)
            verification_note = note_m.group(1).strip() if note_m else ""
            print(f"[{item['id']}] 자체검증 결과 — 확신 수준: {confidence}")

            raw = generate_draft(item, event_year, verification_note, confidence)
            ok = parse_and_save_draft(raw, item, event_year, confidence)
            if ok:
                item["last_published_year"] = event_year
                updated = True
        except Exception as e:
            print(f"[{item['id']}] 처리 실패: {e}")

    if updated:
        save_calendar(data)
        print("\ncalendar_schema.json의 last_published_year 갱신 완료")


if __name__ == "__main__":
    run()
