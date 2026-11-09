"""외국인 중심 화면·검색 노출 — 언어별 경로(/·/en·/ko), 서버가 넣는 언어별 메타(SNS·검색엔진), robots·sitemap·manifest,
영어 방침/약관, 메일 속 사이트 링크 언어. 외부 서비스 없음."""

import pytest
from fastapi.testclient import TestClient

from apps import notifier, store
from apps.api.main import app

client = TestClient(app, follow_redirects=False)
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
    assert r.status_code == 200 and '<html lang="ko">' in r.text and 'window.FINDIT_LANG = "ko"' in r.text


def test_crawlers_are_not_redirected():
    r = client.get("/", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)", "accept-language": "en"})
    assert r.status_code == 200


def test_english_page_has_english_meta_and_hreflang():
    r = client.get("/en", headers=EN_BROWSER)
    t = r.text
    assert r.status_code == 200 and '<html lang="en">' in t and 'window.FINDIT_LANG = "en"' in t
    assert "Lost something in Korea?" in t and 'og:image' in t and 'hreflang="ko"' in t
    assert 'rel="canonical" href="https://findit-lost.duckdns.org/en"' in t
    assert "<!--HEAD-->" not in t
    assert store.metrics_since("2000-01-01").get("view_en") == 1


def test_korean_route_never_redirects():
    assert client.get("/ko", headers=EN_BROWSER).status_code == 200


def test_robots_sitemap_manifest():
    robots = client.get("/robots.txt").text
    assert "Sitemap: https://findit-lost.duckdns.org/sitemap.xml" in robots and "Disallow: /lost-items" in robots
    sm = client.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert "<loc>https://findit-lost.duckdns.org/en</loc>" in sm.text and 'hreflang="en"' in sm.text
    mf = client.get("/manifest.webmanifest")
    assert mf.json()["short_name"] == "FindIt" and mf.headers["content-type"].startswith("application/manifest+json")


def test_english_policy_pages():
    assert "Privacy Policy" in client.get("/en/privacy").text
    assert "Terms of Service" in client.get("/en/terms").text
    for path in ("/static/og.png", "/static/icon-192.png", "/static/icon-512.png"):
        assert client.get(path).status_code == 200


def test_email_site_link_follows_reporter_language():
    assert notifier._site({"lang": "ko"}) == "https://findit-lost.duckdns.org"
    assert notifier._site({"lang": "ja"}) == "https://findit-lost.duckdns.org/en"


def test_english_guide_page_in_sitemap():
    r = client.get("/en/guide", headers=EN_BROWSER)
    assert r.status_code == 200 and "Lost something in Korea?" in r.text and "1330" in r.text
    assert "<loc>https://findit-lost.duckdns.org/en/guide</loc>" in client.get("/sitemap.xml").text
    assert store.metrics_since("2000-01-01").get("view_guide") == 1


def test_search_box_comes_first_and_form_is_minimal():
    """물건을 막 잃어버린 사람이 바로 입력하게: 검색창이 소개보다 위, 첫 폼엔 이메일 칸 없음(알림은 결과 화면에서)."""
    t = client.get("/ko", headers=KO_BROWSER).text
    body = t[t.index("<body>"):]
    assert body.index('id="text"') < body.index('data-i18n="f1t"')
    assert 'id="email"' not in body and 'id="goImg"' not in body      # 사진 검색도 같은 버튼 하나로
    assert 'aria-live="polite"' in body                                # 진행 상황을 화면낭독기에도
