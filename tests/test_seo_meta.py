"""검색 노출 — 제목·설명이 검색 결과에서 잘리지 않는 길이인지, 사이트맵에 수정 날짜(lastmod)가 맞게 붙는지."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

import pytest
from fastapi.testclient import TestClient

from apps.api import site
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app, follow_redirects=False)
BASE = settings.public_base_url.rstrip("/")


def _width(s: str) -> int:
    """검색 결과 표시 폭 어림 — 한글·한자 2칸, 나머지 1칸(구글은 글자 수가 아니라 픽셀로 자름)."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


@pytest.fixture(autouse=True)
def _fresh_cache():
    site._LATEST_FOUND[:] = [0.0, ""]
    yield
    site._LATEST_FOUND[:] = [0.0, ""]


@pytest.mark.parametrize("page,lang", [(p, lg) for p, m in site.PAGES.items() for lg in m])
def test_titles_and_descriptions_fit_search_results(page, lang):
    m = site.PAGES[page][lang]
    assert _width(m["title"]) <= 60, m["title"]
    assert _width(m["desc"]) <= 160, m["desc"]
    assert site.BRAND in m["title"]


def test_home_titles_lead_with_search_words():
    assert site.PAGES["home"]["ko"]["title"].startswith("분실물 찾기")
    assert site.PAGES["home"]["en"]["title"].startswith("Lost something in Korea?")
    t = client.get("/ko").text
    assert f"<title>{site.PAGES['home']['ko']['title']}</title>" in t
    assert f'<meta name="description" content="{site.PAGES["home"]["ko"]["desc"]}" />' in t


def test_every_page_has_a_real_update_date():
    assert set(site.UPDATED) == set(site.PAGES)
    for d in site.UPDATED.values():
        assert date.fromisoformat(d) <= date.today()


def test_sitemap_has_lastmod_for_every_url(monkeypatch):
    monkeypatch.setattr(site, "_latest_found_date", lambda: "")
    xml = client.get("/sitemap.xml").text
    urls = re.findall(r"<url>(.*?)</url>", xml)
    assert len(urls) == sum(len(m) for m in site.PAGES.values())
    for u in urls:
        assert re.search(r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>", u), u
    assert f"<loc>{BASE}/en/guide</loc><lastmod>{site.UPDATED['guide']}</lastmod>" in xml


def test_browse_lastmod_follows_newest_found_item(monkeypatch):
    """목록이 매일 바뀌므로 전체 습득물은 최근 습득일 — 템플릿 날짜보다 늦을 때만, 오늘을 넘지 않게."""
    monkeypatch.setitem(site.UPDATED, "browse", "2026-11-15")
    monkeypatch.setattr(site, "_latest_found_date", lambda: "2026-11-16")
    assert site.lastmod("browse") == "2026-11-16"
    monkeypatch.setattr(site, "_latest_found_date", lambda: "2026-10-01")
    assert site.lastmod("browse") == "2026-11-15"
    monkeypatch.setattr(site, "_latest_found_date", lambda: "2999-01-01")   # 잘못된 미래 날짜는 오늘로
    assert site.lastmod("browse") == site.datetime.now(site._KST).date().isoformat()
    monkeypatch.setattr(site, "_latest_found_date", lambda: "")              # 색인을 못 읽으면 템플릿 날짜
    assert site.lastmod("browse") == "2026-11-15"
    assert site.lastmod("home") == site.UPDATED["home"]                      # 다른 페이지는 색인과 무관


def test_latest_found_date_reads_index_once_then_caches(monkeypatch):
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"hits": {"hits": [{"_source": {"found_at": "2026-11-16T00:00:00"}}]}}'

    monkeypatch.setattr(site.urllib.request, "urlopen", lambda req, timeout=0: calls.append(timeout) or Resp())
    assert site._latest_found_date() == "2026-11-16"
    assert site._latest_found_date() == "2026-11-16"
    assert calls == [3]                                                   # 짧게 기다리고, 한 시간은 다시 안 물음


def test_latest_found_date_failure_does_not_break_sitemap(monkeypatch):
    def down(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(site.urllib.request, "urlopen", down)
    assert site._latest_found_date() == ""
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and f"<lastmod>{site.UPDATED['browse']}</lastmod>" in r.text
