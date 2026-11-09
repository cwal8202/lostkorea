"""웹사이트 페이지 — 홈(검색)·전체 습득물·가이드·개인정보처리방침·약관을 언어별(한/영)로 서버에서 그린다.

- 모든 페이지가 같은 머리글·바닥글(partials/)을 쓰고, 페이지 글자는 서버가 언어에 맞게 채운다
  (검색엔진·SNS 미리보기는 JS를 안 돌리거나 늦게 돌리므로 — 화면에 다른 언어가 잠깐 비치는 일도 없음).
- 템플릿 자리표시자: `<!--HEAD-->`(제목·메타·OG·hreflang·JSON-LD) · `<!--HEADER-->` · `<!--FOOTER-->` · `{{키}}`(문구·링크).
- 정적 자산(css·js)은 내용 해시를 `?v=`로 붙여 배포 즉시 새 파일을 받게 한다.
- 서비스 이름은 `settings.brand_name` 하나로(로고 글자 Lost·Korea는 partials에 직접).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from apps import store

from .config import settings

router = APIRouter()
WEB = Path(__file__).resolve().parents[2] / "apps" / "web"
BRAND = settings.brand_name
REPO_ISSUES = "https://github.com/cwal8202/findit/issues"

_BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|scan|curl|wget|python|httpx|go-http|java/|headless|"
                     r"facebookexternalhit|kakaotalk-scrap|monitor|uptime", re.I)


def is_bot(request: Request) -> bool:
    ua = request.headers.get("user-agent", "")
    return not ua or bool(_BOT_RE.search(ua))


def _asset_version() -> str:
    h = hashlib.md5()
    for p in ("static/css/site.css", "static/js/app.js", "static/js/site.js", "static/fonts/pretendard/pretendard.css"):
        f = WEB / p
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


VERSION = _asset_version()

# ── 페이지 메타(제목·설명·경로). path는 언어별 주소, alt는 다른 언어 주소 ──
PAGES: dict[str, dict[str, dict[str, str]]] = {
    "home": {
        "ko": {"path": "/", "title": f"{BRAND} — 한국 분실물 AI 찾기 | 경찰서·유실물센터 습득물 검색",
               "desc": "잃어버린 물건을 말로 설명하거나 사진을 올리면, AI가 전국 경찰서·유실물센터 습득물 공개데이터에서 찾아드려요. "
                       "보관 장소·연락처 안내, 새 습득물이 들어오면 이메일 알림까지. 무료·회원가입 없음."},
        "en": {"path": "/en", "title": f"{BRAND} — Lost something in Korea? AI lost & found search",
               "desc": f"Lost an item in Korea? Describe it in any language or upload a photo. {BRAND} searches Korea's "
                       "police and public lost-and-found data with AI, shows where your item is held and how to get it "
                       "back, and emails you when a match turns up. Free, no sign-up."},
    },
    "browse": {
        "ko": {"path": "/browse", "title": f"전체 습득물 둘러보기 — {BRAND}",
               "desc": "전국 경찰서·유실물센터에 들어온 습득물을 키워드·지역·날짜로 바로 살펴보세요. 매일 자정 갱신."},
        "en": {"path": "/en/browse", "title": f"Browse all found items in Korea — {BRAND}",
               "desc": "Browse items handed in to police and lost-and-found offices across Korea by keyword, area and date. "
                       "Updated daily."},
    },
    "guide": {
        "en": {"path": "/en/guide", "title": f"Lost something in Korea? What to do, step by step — {BRAND}",
               "desc": "Lost your wallet, phone or passport in Korea? A practical step-by-step guide: where lost items go, "
                       "how to search Korea's lost-and-found data in English, getting help with Korean phone calls, "
                       "and what to bring to pick it up."},
    },
    "privacy": {
        "ko": {"path": "/privacy", "title": f"개인정보처리방침 — {BRAND}", "desc": f"{BRAND} 개인정보처리방침"},
        "en": {"path": "/en/privacy", "title": f"Privacy Policy — {BRAND}", "desc": f"{BRAND} Privacy Policy"},
    },
    "terms": {
        "ko": {"path": "/terms", "title": f"이용약관 — {BRAND}", "desc": f"{BRAND} 이용약관"},
        "en": {"path": "/en/terms", "title": f"Terms of Service — {BRAND}", "desc": f"{BRAND} Terms of Service"},
    },
}
_TEMPLATE = {"home": "index.html", "browse": "index.html", "guide": "guide_en.html",
             "privacy": "privacy.html", "terms": "terms.html"}
_TEMPLATE_EN = {"privacy": "privacy_en.html", "terms": "terms_en.html"}
_LOCALE = {"ko": "ko_KR", "en": "en_US"}

# ── 화면 문구(서버가 채움). 동적 문구(검색 진행·결과)는 static/js/app.js의 I18N ──
T: dict[str, dict[str, str]] = {
    "ko": {
        "brand_tag": "한국 분실물 AI 찾기",
        "nav_find": "분실물 찾기", "nav_browse": "전체 습득물", "nav_how": "이용 방법", "nav_faq": "자주 묻는 질문",
        "nav_guide": "", "nav_cta": "지금 찾기", "nav_menu": "메뉴",
        "hero_eyebrow": "경찰청·공공데이터 기반 · 무료 · 회원가입 없음",
        "hero_title": "잃어버린 물건,<br>AI가 대신 찾아드려요",
        "hero_lede": "전국 경찰서·유실물센터에 들어온 습득물을 <b>말이나 사진으로</b> 찾아요. "
                     "어떤 언어로 써도 되고, 찾으면 보관 장소와 연락처까지 안내해요.",
        "search_label": "무엇을, 어디서, 언제 잃어버렸나요?",
        "text_ph": "예: 어제 2호선 홍대입구역 근처에서 검정 가죽 지갑을 잃어버렸어요. 안에 신분증이랑 카드가 있었어요.",
        "photo_add": "사진 추가", "photo_remove": "사진 빼기", "lost_date": "분실일(선택)", "search_btn": "찾기",
        "photo_tip_title": "사진으로 찾을 때 알아두세요",
        "photo_tip": "습득물은 사진이 없는 경우가 많아요(약 22%만 보유). 그래서 사진 속 <b>종류·색상을 읽어 글로 검색</b>하고, "
                     "후보 중 <b>사진이 있는 건 당신 사진과 직접 비교</b>해요. 설명·장소를 함께 적을수록 정확해져요.",
        "privacy_note": "이름·전화번호 등 개인정보는 적지 마세요. 신고 내용·사진은 매칭을 위해 Google Gemini로 분석되고(사진은 저장 안 함), "
                        "신고는 알림 기간이 끝나고 30일 뒤 자동 삭제돼요. 검색하면 <a href=\"/privacy\">개인정보처리방침</a>·"
                        "<a href=\"/terms\">이용약관</a>에 동의한 것으로 봐요.",
        "trust1": "경찰청·공공데이터포털 공식 데이터", "trust2": "매일 자정 갱신", "trust3": "영어 화면 · 13개 언어 알림",
        "sample_badge": "예시 결과", "sample_item": "검정 가죽 지갑", "sample_color": "검정", "sample_score": "일치",
        "sample_meta": "보관: 마포경찰서 · 습득일 11월 8일",
        "sample_reason": "색·종류가 같고, 잃어버린 홍대입구역 인근(마포구)에서 습득됐어요.",
        "sample_mine": "내 물건이에요", "sample_map": "지도",
        "result_title": "매칭 결과", "mine_title": "내 신고 (이 브라우저)", "refresh": "새로고침",
        "mine_lead": "딱 맞는 게 없어도 신고는 저장돼요. <b>새 습득물이 들어오면 매일 다시 찾아</b> 이메일로 알려드려요. "
                     "내 신고는 이 브라우저에서만 보여요.",
        "stat_items": "검색할 수 있는 습득물", "stat_latest": "가장 최근 습득일", "stat_update": "갱신", "stat_update_v": "매일 00시",
        "stat_cost": "이용료", "stat_cost_v": "무료",
        "how_title": "이렇게 찾아드려요", "how_lede": "복잡한 검색 조건 없이, 잃어버린 상황을 말하듯 적으면 돼요.",
        "how1_t": "설명하거나 사진 올리기",
        "how1_d": "‘어제 2호선에서 검정 지갑을 잃어버렸어요’처럼 평소 말하듯 적거나, 물건 사진을 올리세요.",
        "how2_t": "AI가 후보와 근거 제시",
        "how2_d": "의미로 비교해 같은 물건일 가능성이 높은 습득물을 점수·이유와 함께 보여드려요. 확인은 본인이 해요.",
        "how3_t": "보관 기관에서 찾기",
        "how3_d": "보관 장소·전화번호·지도를 안내해요. 신분증(외국인은 여권)을 챙겨 방문하면 돼요.",
        "feat_title": f"왜 {BRAND}인가요?",
        "feat1_t": "말하듯 검색", "feat1_d": "물품명을 정확히 몰라도 돼요. 영어·일본어 등 어떤 언어로 써도 한국어 습득물과 맞춰 찾아요.",
        "feat2_t": "이동 경로까지 고려",
        "feat2_d": "지하철·버스로 이동했다면 노선이 지나는 지역까지 넓혀서 찾아요. 분실물은 다른 곳에서 발견되는 일이 많거든요.",
        "feat3_t": "사진으로도 찾기", "feat3_d": "사진 속 종류·색상을 읽어 검색하고, 사진이 있는 습득물은 직접 비교해요.",
        "feat4_t": "놓치지 않게 알림", "feat4_d": "지금 없어도 신고는 저장돼요. 새 습득물이 들어오면 매일 다시 찾아 이메일로 알려드려요.",
        "trust_title": "믿고 쓸 수 있도록",
        "trust_a_t": "공식 데이터만",
        "trust_a_d": "경찰청 유실물 통합포털(LOST112)과 공공데이터포털에 공개된 습득물만 다뤄요. 매일 자정 새 습득물을 반영해요.",
        "trust_b_t": "개인정보는 최소로",
        "trust_b_d": "회원가입이 없고 사진은 저장하지 않아요. 신고는 알림 기간이 끝나면 자동 삭제되고, 언제든 직접 지울 수 있어요.",
        "trust_c_t": "확인은 언제나 본인이",
        "trust_c_d": "AI는 후보를 추천할 뿐이에요. 같은 물건인지는 직접 확인하고, 수령은 보관 기관의 본인 확인 절차를 따라요.",
        "faq_title": "자주 묻는 질문",
        "cta_title": "지금 바로 찾아보세요", "cta_lede": "1분이면 충분해요. 회원가입 없이 무료로 쓸 수 있어요.", "cta_btn": "분실물 찾기",
        "browse_title": "전체 습득물 둘러보기",
        "browse_lede": "공개데이터로 모은 전국 습득물을 키워드·지역·날짜로 바로 살펴보세요. (AI 판정 없이 즉시 조회)",
        "kw_ph": "키워드 (예: 지갑, 에어팟, 카드)", "all_regions": "전체 지역", "found_date": "습득일", "search_short": "검색",
        "reset": "초기화", "per_page": "개씩", "loading": "불러오는 중…",
        "f_tagline": "한국에서 잃어버린 물건, AI가 공공 습득물 데이터에서 찾아드려요.",
        "f_service": "서비스", "f_info": "정보", "f_contact": "문의",
        "f_guide": "", "f_privacy": "개인정보처리방침", "f_terms": "이용약관", "f_issues": "문의하기 (GitHub)",
        "f_source": "습득물 정보 출처: 경찰청 유실물 통합포털(LOST112) · 공공데이터포털",
        "f_copy": f"© 2026 {BRAND} · 개인이 운영하는 서비스입니다", "f_other_lang": "English",
        "back_home": f"{BRAND} 홈으로",
    },
    "en": {
        "brand_tag": "AI lost & found for Korea",
        "nav_find": "Find my item", "nav_browse": "Browse found items", "nav_how": "How it works", "nav_faq": "FAQ",
        "nav_guide": "Guide", "nav_cta": "Search now", "nav_menu": "Menu",
        "hero_eyebrow": "Official public data · Free · No sign-up",
        "hero_title": "Lost something in Korea?<br>We'll help you find it.",
        "hero_lede": "Describe your item <b>in any language</b> or add a photo. We search items handed in to police and "
                     "lost-and-found offices across Korea — and show you where it's held and who to call.",
        "search_label": "What did you lose, where and when?",
        "text_ph": "e.g., I lost a black leather wallet near Hongdae station yesterday. My ID and cards were inside. "
                   "— A station, area or landmark is enough.",
        "photo_add": "Add photo", "photo_remove": "Remove photo", "lost_date": "Date lost (optional)", "search_btn": "Search",
        "photo_tip_title": "Searching with a photo — good to know",
        "photo_tip": "Many found items have no photo (only ~22% do). So we <b>read your photo's type and color and search by "
                     "text</b>, and <b>compare directly</b> with candidates that do have a photo. Adding a description and "
                     "place improves accuracy.",
        "privacy_note": "Please don't include names or phone numbers. Your text/photo is analyzed by Google Gemini for matching "
                        "(photos are not stored), and reports are deleted automatically 30 days after the alert period ends. "
                        "By searching you agree to the <a href=\"/en/privacy\">Privacy Policy</a> and "
                        "<a href=\"/en/terms\">Terms</a>.",
        "trust1": "Official data from Korean police", "trust2": "Updated daily", "trust3": "Alerts in 13 languages",
        "sample_badge": "Example result", "sample_item": "Black leather wallet", "sample_color": "black",
        "sample_score": "match", "sample_meta": "Held at: Mapo Police Station · found Nov 8",
        "sample_reason": "Same color and type, and found near Hongdae station (Mapo-gu), where you lost it.",
        "sample_mine": "This is mine", "sample_map": "Map",
        "result_title": "Results", "mine_title": "My reports (this browser)", "refresh": "Refresh",
        "mine_lead": "No exact match yet? Your report is saved. <b>We re-check every day as new items arrive</b> and email you. "
                     "Your reports are visible only in this browser.",
        "stat_items": "Found items you can search", "stat_latest": "Newest item found", "stat_update": "Updated",
        "stat_update_v": "Daily", "stat_cost": "Cost", "stat_cost_v": "Free",
        "how_title": "How it works", "how_lede": "No search forms — just tell us what happened, the way you'd say it.",
        "how1_t": "Describe it or add a photo",
        "how1_d": "Write it the way you'd say it — \"I left my black wallet on the subway near Hongdae\" — or upload a photo.",
        "how2_t": "AI shows candidates + reasons",
        "how2_d": "We match by meaning and show found items most likely to be yours, with a score and the reason. You decide.",
        "how3_t": "Pick it up",
        "how3_d": "We show where it's held, the phone number and a map. Bring your passport or ID when you visit.",
        "feat_title": f"Why {BRAND}?",
        "feat1_t": "Search in any language",
        "feat1_d": "No need to know the Korean word. Write in English, Japanese or any language — we match it to Korean records.",
        "feat2_t": "Follows your route",
        "feat2_d": "Items often travel along a subway or bus line. We also search the districts your route passes through.",
        "feat3_t": "Search with a photo",
        "feat3_d": "We read the type and color from your photo, and compare directly with found items that have photos.",
        "feat4_t": "Alerts when it turns up",
        "feat4_d": "Nothing yet? Your report is saved. We re-check daily as new items arrive and email you.",
        "trust_title": "Built to be trusted",
        "trust_a_t": "Official data only",
        "trust_a_d": "We only use found items published by Korea's National Police Agency (LOST112) and the Public Data "
                     "Portal, refreshed every night.",
        "trust_b_t": "Minimal personal data",
        "trust_b_d": "No sign-up, and photos are never stored. Reports are deleted automatically after the alert period, and "
                     "you can delete them anytime.",
        "trust_c_t": "You always confirm",
        "trust_c_d": "AI only suggests candidates. You check whether it's yours, and the holding office verifies your identity.",
        "faq_title": "Frequently asked questions",
        "cta_title": "Start your search now", "cta_lede": "It takes a minute. Free, no sign-up.", "cta_btn": "Find my item",
        "browse_title": "Browse all found items",
        "browse_lede": "Browse items handed in across Korea by keyword, area and date. (Instant — no AI grading.)",
        "kw_ph": "Keyword (e.g., wallet, AirPods, card)", "all_regions": "All regions", "found_date": "Found date",
        "search_short": "Search", "reset": "Reset", "per_page": "per page", "loading": "Loading…",
        "f_tagline": "Lost something in Korea? AI searches public lost-and-found data for you.",
        "f_service": "Service", "f_info": "Info", "f_contact": "Contact",
        "f_guide": "Guide: lost something in Korea?", "f_privacy": "Privacy Policy", "f_terms": "Terms of Service",
        "f_issues": "Contact us (GitHub)",
        "f_source": "Found-item data: Korean National Police Agency LOST112 · Public Data Portal",
        "f_copy": f"© 2026 {BRAND} · An independently run service", "f_other_lang": "한국어",
        "back_home": f"Back to {BRAND}",
    },
}

FAQ: dict[str, list[tuple[str, str]]] = {
    "ko": [
        ("무료인가요?", "네. 회원가입 없이 무료로 이용할 수 있어요."),
        ("어떤 습득물을 찾을 수 있나요?",
         "경찰청 유실물 통합포털(LOST112)과 공공데이터포털에 공개된 전국 경찰서·유실물센터 습득물이에요. 매일 자정에 새 습득물을 "
         "모아 최대 6개월 치를 검색해요. 지하철역·가게 등에서 보관 중이라 아직 등록되지 않은 물건은 없을 수 있어요."),
        ("찾으면 어떻게 받나요?",
         "결과에 나온 보관 기관에 전화해 보관 여부를 확인한 뒤, 신분증(외국인은 여권)을 챙겨 방문하세요. 지갑 속 카드처럼 "
         f"내 물건임을 보여줄 특징을 말할 수 있으면 좋아요. {BRAND}는 물건을 보관하거나 배송하지 않아요."),
        ("지금 결과에 없으면요?",
         "습득물은 습득 후 며칠에 걸쳐 등록돼요. 검색하면 신고가 저장되고, 이메일을 남기면 알림 기간(기본 30일, 최대 180일) 동안 "
         "매일 새 습득물과 비교해 유력 후보가 나오면 알려드려요."),
        ("외국인도 쓸 수 있나요?",
         "네. 영어 화면을 제공하고 어떤 언어로 써도 검색돼요. 판정 근거와 알림 메일도 쓰신 언어로 보내요. 보관 기관과 통화가 "
         "어려우면 1330 관광통역안내전화(24시간)를 이용하세요."),
        ("점수는 무슨 뜻인가요?",
         "AI가 추정한 '같은 물건일 가능성'(0~100)이에요. 80점 이상은 유력 후보, 50~79점은 비슷한 후보예요. 점수가 높아도 "
         "확정은 아니니 직접 확인하세요."),
        ("개인정보는 안전한가요?",
         "회원가입이 없고 사진은 저장하지 않아요. 신고 내용은 알림 기간이 끝나고 30일 뒤 자동 삭제되며, '내 신고'에서 바로 지울 "
         "수도 있어요. 자세한 내용은 개인정보처리방침을 확인하세요."),
    ],
    "en": [
        ("Is it free?", "Yes. It's free and needs no sign-up."),
        ("What items can I find?",
         "Items handed in to police stations and lost-and-found offices across Korea and published on the National Police "
         "Agency's LOST112 portal and the Public Data Portal. We collect new items every night and search up to six months. "
         "Items still held at a station or shop and not yet registered may not appear."),
        ("How do I get my item back?",
         "Call the holding office shown in the result to confirm they have it, then visit with your passport or ID. It helps "
         f"to describe something only the owner would know (e.g., the cards in a wallet). {BRAND} doesn't keep or deliver items."),
        ("What if it's not in the results yet?",
         "Found items are registered over several days. Your search is saved as a report; leave an email and we'll compare it "
         "with new items every day during the alert period (30 days by default, up to 180) and email you when a strong "
         "candidate appears."),
        ("I don't speak Korean. Can I use it?",
         "Yes. The site is in English, and you can search in any language. Match reasons and alert emails come in your "
         "language. If calling the office is hard, call 1330, the Korea Travel Helpline (24/7), for interpretation."),
        ("What does the score mean?",
         "It's the AI's estimate (0–100) that the found item is yours. 80+ is a strong candidate, 50–79 is similar. "
         "Even a high score isn't certain, so please check yourself."),
        ("Is my personal data safe?",
         "No sign-up, and photos are never stored. Reports are deleted automatically 30 days after the alert period ends, and "
         "you can delete them anytime under \"My reports\". See the Privacy Policy for details."),
    ],
}

_LANG_OPTIONS = [("ko", "한국어"), ("en", "English"), ("ja", "日本語"), ("zh-CN", "中文 (简体)"), ("zh-TW", "中文 (繁體)"),
                 ("vi", "Tiếng Việt"), ("th", "ไทย"), ("id", "Bahasa Indonesia"), ("ms", "Bahasa Melayu"),
                 ("hi", "हिन्दी"), ("fr", "Français"), ("es", "Español"), ("ru", "Русский"), ("de", "Deutsch")]


def _url(page: str, lang: str) -> str:
    meta = PAGES[page]
    return (meta.get(lang) or meta.get("en") or meta["ko"])["path"]


def _head(page: str, lang: str) -> str:
    m, base = PAGES[page][lang], settings.public_base_url.rstrip("/")
    e = html.escape
    url = base + m["path"]
    lines = [
        f"<title>{e(m['title'])}</title>",
        f'<meta name="description" content="{e(m["desc"])}" />',
        f'<link rel="canonical" href="{url}" />',
    ]
    for alt_lang, alt in PAGES[page].items():
        lines.append(f'<link rel="alternate" hreflang="{alt_lang}" href="{base}{alt["path"]}" />')
    if len(PAGES[page]) > 1:
        lines.append(f'<link rel="alternate" hreflang="x-default" href="{base}{_url(page, "en")}" />')
    lines += [
        '<meta property="og:type" content="website" />',
        f'<meta property="og:site_name" content="{BRAND}" />',
        f'<meta property="og:title" content="{e(m["title"])}" />',
        f'<meta property="og:description" content="{e(m["desc"])}" />',
        f'<meta property="og:url" content="{url}" />',
        f'<meta property="og:image" content="{base}/static/og.png" />',
        '<meta property="og:image:width" content="1200" /><meta property="og:image:height" content="630" />',
        f'<meta property="og:locale" content="{_LOCALE[lang]}" />',
        '<meta name="twitter:card" content="summary_large_image" />',
        '<link rel="icon" type="image/svg+xml" href="/static/logo.svg" />',
        '<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png" />',
        '<link rel="apple-touch-icon" href="/static/icon-192.png" />',
        '<link rel="manifest" href="/manifest.webmanifest" />',
        '<meta name="theme-color" content="#1d4ed8" />',
        f'<link rel="stylesheet" href="/static/fonts/pretendard/pretendard.css?v={VERSION}" />',
        f'<link rel="stylesheet" href="/static/css/site.css?v={VERSION}" />',
    ]
    if page == "home":
        ld = [{"@context": "https://schema.org", "@type": "WebApplication", "name": BRAND, "url": url,
               "inLanguage": lang, "applicationCategory": "UtilitiesApplication", "operatingSystem": "Any",
               "description": m["desc"], "offers": {"@type": "Offer", "price": "0", "priceCurrency": "KRW"}},
              {"@context": "https://schema.org", "@type": "FAQPage",
               "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                              for q, a in FAQ[lang]]}]
        lines.append(f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>')
    lines.append(f'<script>window.LK_LANG = "{lang}"; window.LK_VIEW = "{page}";</script>')
    return "\n".join(lines)


def _faq_html(lang: str) -> str:
    return "\n".join(f'<details class="faq-item"><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>'
                     for q, a in FAQ[lang])


def _lang_options(page: str, lang: str, app: bool) -> str:
    opts = _LANG_OPTIONS if app else _LANG_OPTIONS[:2]
    return "".join(f'<option value="{c}"{" selected" if c == lang else ""}>{n}</option>' for c, n in opts)


def _fill(text: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"템플릿 자리표시자 값 없음: {key}")
        return values[key]
    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", sub, text)


def render(page: str, lang: str) -> str:
    """템플릿 + 언어 → 완성된 HTML."""
    app = page in ("home", "browse")
    tpl = (_TEMPLATE_EN.get(page) if lang == "en" else None) or _TEMPLATE[page]
    src = (WEB / tpl).read_text(encoding="utf-8")
    home = _url("home", lang)
    other = "en" if lang == "ko" else "ko"
    values = {
        **T[lang],
        "BRAND": BRAND, "V": VERSION, "LANG": lang, "VIEW": page,
        "HOME": home, "BROWSE": _url("browse", lang), "GUIDE": "/en/guide",
        "PRIVACY": _url("privacy", lang), "TERMS": _url("terms", lang), "ISSUES": REPO_ISSUES,
        "ALT_KO": _url(page, "ko") if "ko" in PAGES[page] else "/", "ALT_EN": _url(page, "en"),
        "OTHER_LANG_URL": (_url(page, other) if other in PAGES[page] else _url("home", other)),
        "OTHER_LANG": other,
        "LANG_OPTIONS": _lang_options(page, lang, app),
        "FAQ_ITEMS": _faq_html(lang),
        "GUIDE_NAV": (f'<a href="/en/guide">{T[lang]["nav_guide"]}</a>' if lang == "en" else ""),
        "GUIDE_FOOT": (f'<li><a href="/en/guide">{T[lang]["f_guide"]}</a></li>' if lang == "en" else ""),
    }
    header = _fill((WEB / "partials" / "header.html").read_text(encoding="utf-8"), values)
    footer = _fill((WEB / "partials" / "footer.html").read_text(encoding="utf-8"), values)
    out = src.replace('<html lang="ko">', f'<html lang="{lang}">', 1).replace('<html lang="en">', f'<html lang="{lang}">', 1)
    out = out.replace("<!--HEAD-->", _head(page, lang), 1).replace("<!--HEADER-->", header, 1)
    out = out.replace("<!--FOOTER-->", footer, 1)
    return _fill(out, values)


def _page(request: Request, page: str, lang: str, count: str | None = None) -> HTMLResponse:
    if count and not is_bot(request):
        store.bump(count)
    return HTMLResponse(render(page, lang), headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                                     "Vary": "Accept-Language"})


def _prefers_korean(request: Request) -> bool:
    """브라우저 언어 목록에 한국어가 있으면(또는 언어 정보가 없으면 — 검색엔진 봇 등) 한국어."""
    al = request.headers.get("accept-language", "").lower()
    return not al or any(part.strip().startswith("ko") for part in al.split(","))


@router.get("/", include_in_schema=False)
def index(request: Request):
    # 외국어 브라우저 → 영어 화면(/en)으로. 봇은 리다이렉트하지 않음(검색엔진은 /와 /en을 hreflang으로 따로 색인).
    if not is_bot(request) and not _prefers_korean(request):
        return RedirectResponse("/en", status_code=302)
    return _page(request, "home", "ko", "view_ko")


@router.get("/ko", include_in_schema=False)
def index_ko(request: Request) -> HTMLResponse:
    """한국어 고정(영어 화면에서 '한국어'를 고른 경우 — 브라우저 언어와 무관)."""
    return _page(request, "home", "ko", "view_ko")


@router.get("/en", include_in_schema=False)
def index_en(request: Request) -> HTMLResponse:
    return _page(request, "home", "en", "view_en")


@router.get("/browse", include_in_schema=False)
def browse_ko(request: Request) -> HTMLResponse:
    return _page(request, "browse", "ko")


@router.get("/en/browse", include_in_schema=False)
def browse_en(request: Request) -> HTMLResponse:
    return _page(request, "browse", "en")


@router.get("/en/guide", include_in_schema=False)
def guide_en(request: Request) -> HTMLResponse:
    """'한국에서 물건을 잃어버렸을 때' 영어 가이드 — 검색 유입용(외국인이 실제로 검색하는 질문에 답하는 페이지)."""
    return _page(request, "guide", "en", "view_guide")


@router.get("/privacy", include_in_schema=False)
def privacy(request: Request) -> HTMLResponse:
    return _page(request, "privacy", "ko")


@router.get("/terms", include_in_schema=False)
def terms(request: Request) -> HTMLResponse:
    return _page(request, "terms", "ko")


@router.get("/en/privacy", include_in_schema=False)
def privacy_en(request: Request) -> HTMLResponse:
    return _page(request, "privacy", "en")


@router.get("/en/terms", include_in_schema=False)
def terms_en(request: Request) -> HTMLResponse:
    return _page(request, "terms", "en")


@router.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    base = settings.public_base_url.rstrip("/")
    return PlainTextResponse("\n".join([
        "User-agent: *", "Allow: /",
        "Disallow: /lost-items", "Disallow: /email/", "Disallow: /found-items/", "Disallow: /health",
        f"Sitemap: {base}/sitemap.xml", "",
    ]))


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    base = settings.public_base_url.rstrip("/")
    urls = []
    for meta in PAGES.values():
        alts = "".join(f'<xhtml:link rel="alternate" hreflang="{lg}" href="{base}{m["path"]}"/>' for lg, m in meta.items()) \
            if len(meta) > 1 else ""
        urls += [f"<url><loc>{base}{m['path']}</loc>{alts}</url>" for m in meta.values()]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">'
           + "".join(urls) + "</urlset>")
    return Response(xml, media_type="application/xml")


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> JSONResponse:
    """홈 화면에 추가(PWA) — 여행 중 다시 열기 쉽게."""
    return JSONResponse({
        "name": f"{BRAND} — Lost & Found Korea", "short_name": BRAND, "start_url": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#1d4ed8",
        "description": PAGES["home"]["en"]["desc"],
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
    }, media_type="application/manifest+json")
