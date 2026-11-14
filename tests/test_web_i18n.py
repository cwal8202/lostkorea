"""외국인 중심 화면·검색 노출 — 언어별 경로(/·/en·/ko), 서버가 넣는 언어별 메타(SNS·검색엔진), robots·sitemap·manifest,
영어 방침/약관, 메일 속 사이트 링크 언어. 외부 서비스 없음."""

import pytest
from fastapi.testclient import TestClient

from apps import notifier, store
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app, follow_redirects=False)
BASE = settings.public_base_url.rstrip("/")
EN_BROWSER = {"user-agent": "Mozilla/5.0 (Macintosh) Safari/605", "accept-language": "en-US,en;q=0.9"}
KO_BROWSER = {"user-agent": "Mozilla/5.0 (Macintosh) Safari/605", "accept-language": "en-US,en;q=0.9,ko;q=0.8"}


@pytest.fixture(autouse=True)
def clean():
    store.init()
    store._run("DELETE FROM metrics")
    yield


def test_foreign_browser_redirected_to_english():
    r = client.get("/", headers=EN_BROWSER)
    assert r.status_code == 302 and r.headers["location"] == "/en"


def test_korean_in_language_list_stays_korean():
    r = client.get("/", headers=KO_BROWSER)
    assert r.status_code == 200 and '<html lang="ko">' in r.text and 'window.LK_LANG = "ko"' in r.text


def test_crawlers_are_not_redirected():
    r = client.get("/", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)", "accept-language": "en"})
    assert r.status_code == 200


def test_english_page_has_english_meta_and_hreflang():
    r = client.get("/en", headers=EN_BROWSER)
    t = r.text
    assert r.status_code == 200 and '<html lang="en">' in t and 'window.LK_LANG = "en"' in t
    assert "Lost something in Korea?" in t and 'og:image' in t and 'hreflang="ko"' in t
    assert f'rel="canonical" href="{BASE}/en"' in t
    assert "<!--HEAD-->" not in t
    assert store.metrics_since("2000-01-01").get("view_en") == 1


def test_korean_route_never_redirects():
    assert client.get("/ko", headers=EN_BROWSER).status_code == 200


def test_robots_sitemap_manifest():
    robots = client.get("/robots.txt").text
    assert f"Sitemap: {BASE}/sitemap.xml" in robots and "Disallow: /lost-items" in robots
    sm = client.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert f"<loc>{BASE}/en</loc>" in sm.text and 'hreflang="en"' in sm.text
    mf = client.get("/manifest.webmanifest")
    assert mf.json()["short_name"] == "LostKorea" and mf.headers["content-type"].startswith("application/manifest+json")


def test_english_policy_pages():
    assert "Privacy Policy" in client.get("/en/privacy").text
    assert "Terms of Service" in client.get("/en/terms").text
    for path in ("/static/og.png", "/static/icon-192.png", "/static/icon-512.png"):
        assert client.get(path).status_code == 200


def test_email_site_link_follows_reporter_language():
    assert notifier._site({"lang": "ko"}) == BASE
    assert notifier._site({"lang": "ja"}) == f"{BASE}/en"


def test_english_guide_page_in_sitemap():
    r = client.get("/en/guide", headers=EN_BROWSER)
    assert r.status_code == 200 and "Lost something in Korea?" in r.text and "1330" in r.text
    assert f"<loc>{BASE}/en/guide</loc>" in client.get("/sitemap.xml").text
    assert store.metrics_since("2000-01-01").get("view_guide") == 1


def test_search_box_comes_first_and_form_is_minimal():
    """물건을 막 잃어버린 사람이 바로 입력하게: 검색창이 소개보다 위, 첫 폼엔 이메일 칸 없음(알림은 결과 화면에서)."""
    t = client.get("/ko", headers=KO_BROWSER).text
    body = t[t.index("<body"):]
    assert body.index('id="text"') < body.index('id="how"')
    assert 'id="email"' not in body and 'id="goImg"' not in body      # 사진 검색도 같은 버튼 하나로
    assert 'aria-live="polite"' in body                                # 진행 상황을 화면낭독기에도


@pytest.mark.parametrize("path,ai_line,policy", [("/ko", "Google AI로 분석", 'href="/privacy"'),
                                                 ("/en", "Analyzed by Google AI", 'href="/en/privacy"')])
def test_privacy_notice_is_short_but_keeps_ai_and_consent_visible(path, ai_line, policy):
    """입력창 아래 개인정보 안내는 체크 3개 + 한 줄로 줄이되, 국외 AI 분석 사실과 방침 동의는 접지 않고 보이게.
    삭제 시점(알림 기간 종료 + 30일)은 '자세히' 안에만 — 칩에 '30일 뒤 삭제'처럼 틀리게 줄여 쓰지 않음."""
    body = client.get(path, headers=KO_BROWSER).text
    card = body[body.index('class="search-card"'):body.index('id="resultCard"')]
    visible, detail = card.split('class="privacy-more"', 1)
    assert ai_line in visible and policy in visible
    assert "30" not in visible[visible.index('class="assure"'):] and "30" in detail
    assert 'id="stickyCta" hidden' in body                              # 휴대폰 하단 고정 버튼(처음엔 숨김)


@pytest.mark.parametrize("path", ["/ko", "/en", "/browse", "/en/browse", "/en/guide", "/privacy", "/terms",
                                  "/en/privacy", "/en/terms"])
def test_every_page_renders_with_shared_header_and_footer(path):
    """모든 페이지가 같은 머리글·바닥글, 자리표시자가 남지 않고, 제목에 서비스 이름."""
    r = client.get(path, headers=EN_BROWSER)
    t = r.text
    assert r.status_code == 200
    assert "{{" not in t and "<!--HEAD-->" not in t and "<!--HEADER-->" not in t and "<!--FOOTER-->" not in t
    assert 'class="site-header"' in t and 'class="site-footer"' in t and "LostKorea" in t.split("</title>")[0]
    assert '/static/css/site.css?v=' in t and 'rel="canonical"' in t


def test_home_is_server_rendered_in_each_language_with_faq_schema():
    ko, en = client.get("/ko", headers=KO_BROWSER).text, client.get("/en", headers=EN_BROWSER).text
    assert "AI가 대신 찾아드려요" in ko and "We'll help you find it." not in ko
    assert "We'll help you find it." in en and "AI가 대신 찾아드려요" not in en
    assert '"@type": "FAQPage"' in en and "Is it free?" in en
    assert 'class="page-home"' in ko


def test_browse_page_opens_browse_view():
    t = client.get("/browse", headers=KO_BROWSER).text
    assert 'class="page-browse"' in t and 'window.LK_VIEW = "browse"' in t and 'id="viewBrowse"' in t


def test_static_assets_served():
    from apps.api.site import VERSION
    for path in ("/static/css/site.css", "/static/js/app.js", "/static/js/site.js", "/static/logo.svg",
                 "/static/logo-email.png", "/static/fonts/pretendard/pretendard.css",
                 "/static/fonts/pretendard/woff2-dynamic-subset/PretendardVariable.subset.0.woff2"):
        assert client.get(path).status_code == 200, path
    assert len(VERSION) == 10


def test_old_host_redirects_to_canonical_address(monkeypatch):
    """주소를 바꿔도 옛 링크가 이어지게 — 옛 호스트는 대표 주소로 영구 이동(GET 301, 그 외 308). 서버 안 점검(localhost)은 그대로."""
    from apps.api import main
    monkeypatch.setattr(main, "_REDIRECT_HOSTS", {"findit-lost.duckdns.org"})
    monkeypatch.setattr(main.settings, "public_base_url", "https://lostkorea.duckdns.org")
    r = client.get("/en/guide?x=1", headers={"host": "findit-lost.duckdns.org", **EN_BROWSER})
    assert r.status_code == 301 and r.headers["location"] == "https://lostkorea.duckdns.org/en/guide?x=1"
    r = client.post("/email/verify", json={"token": "t"}, headers={"host": "findit-lost.duckdns.org"})
    assert r.status_code == 308
    assert client.get("/health", headers={"host": "localhost"}).status_code == 200


def test_search_engine_verification_tag_on_home():
    """네이버 서치어드바이저 소유 확인 — 홈(한국어 그대로) head에 확인 태그가 있어야 확인이 유지된다."""
    from apps.api.site import SITE_VERIFICATION
    t = client.get("/", headers=KO_BROWSER).text
    for name, value in SITE_VERIFICATION.items():
        assert f'<meta name="{name}" content="{value}" />' in t


@pytest.mark.parametrize("path,chapter", [("/ko", "알림 신청"), ("/en", "Get alerts")])
def test_home_has_flow_demo_with_text_alternative(path, chapter):
    """첫 화면 25초 시연: 시연 틀은 화면낭독기에서 숨기고(aria-hidden), 같은 내용을 단계 목록 글로 제공.
    로컬 미리보기 전용 스크립트(예시 데이터)는 운영 페이지에 실리지 않음."""
    body = client.get(path, headers=KO_BROWSER).text
    assert 'id="flowDevice" aria-hidden="true"' in body and 'id="flowDemo"' in body
    assert 'id="flowSteps"' in body and chapter in body and 'id="flowToggle"' in body   # 일시정지 버튼
    assert "/static/js/demo.js?v=" in body and "preview_v2" not in body


@pytest.mark.parametrize("path,photo,sort", [("/browse", "사진 있는 것만", "오래된순"),
                                             ("/en/browse", "With photo", "Oldest first")])
def test_browse_page_has_photo_filter_and_sort(path, photo, sort):
    body = client.get(path, headers=KO_BROWSER).text
    assert 'id="bphoto"' in body and photo in body and 'value="with"' in body and 'value="without"' in body
    assert 'id="bsort"' in body and sort in body and 'value="old"' in body


@pytest.mark.parametrize("path", ["/ko", "/en", "/privacy", "/terms", "/en/privacy", "/en/terms"])
def test_contact_is_email_not_github(path):
    t = client.get(path, headers=KO_BROWSER).text
    assert "mailto:help@lostkorea.com" in t and "github.com/cwal8202/lostkorea/issues" not in t
    if "privacy" in path:
        assert "mailto:privacy@lostkorea.com" in t and "최장호" in t     # 개인정보 보호책임자 성명(개인정보보호법)
