"""운영 관측 — 익명 통계(일·종류별 횟수), 오류 알림(종류별 1시간 1통), 주간 요약, 외부 감시용 심층 헬스체크.

외부 서비스 없음: 메일(ops.alert)·OpenSearch는 목킹.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from apps import ops, store
from apps.api import main
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app, raise_server_exceptions=False)
BROWSER = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"}


@pytest.fixture(autouse=True)
def clean():
    store.init()
    store._run("DELETE FROM metrics")
    store._run("DELETE FROM lost_items")
    ops._ERR_LAST.clear()
    yield


@pytest.fixture
def mails(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    sent = []
    monkeypatch.setattr(ops, "alert", lambda subject, body: sent.append((subject, body)) or True)
    monkeypatch.setattr(ops.threading, "Thread",  # 백그라운드 발송을 즉시 실행으로(테스트 결정성)
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    return sent


def test_bump_accumulates_per_day_and_kind():
    store.bump("view_ko", day="2026-11-01")
    store.bump("view_ko", day="2026-11-01")
    store.bump("view_ko", 3, day="2026-11-02")
    store.bump("report_text", day="2026-10-31")
    assert store.metrics_since("2026-11-01") == {"view_ko": 5}


def test_page_view_counted_for_browsers_not_bots():
    client.get("/", headers=BROWSER)
    client.get("/", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    client.get("/", headers={"user-agent": "curl/8.0"})
    assert store.metrics_since("2000-01-01").get("view_ko") == 1


def test_rate_limited_requests_are_counted(monkeypatch):
    monkeypatch.setattr(settings, "limit_alerts_hour", 0)
    r = client.post("/email/verify", json={"token": "x"})
    assert r.status_code == 429
    assert store.metrics_since("2000-01-01").get("rate_limited") == 1


def test_unhandled_error_alerts_admin_once_per_hour(monkeypatch, mails):
    def boom(*a, **k):
        raise RuntimeError("opensearch down")

    monkeypatch.setattr(main.search_mod, "browse", boom)
    assert client.get("/found-items/browse").status_code == 500
    assert client.get("/found-items/browse").status_code == 500   # 같은 오류 → 메일은 1통만
    assert len(mails) == 1 and "RuntimeError" in mails[0][0] and "opensearch down" in mails[0][1]
    assert store.metrics_since("2000-01-01").get("error") == 2      # 통계는 매번


def test_weekly_summary(mails):
    store.bump("view_en", 12, day="2026-11-05")
    store.bump("report_text", 3, day="2026-11-06")
    store.bump("view_en", 99, day="2026-10-01")  # 기간 밖
    store.add("검정 지갑", {}, [], [], None, None)
    body = ops.weekly(7, today=date(2026, 11, 9), status={"count": 170581, "latest_found_at": "2026-11-08"})
    assert "방문(영어): 12" in body and "신고(글): 3" in body and "찾는 중 1" in body and "170581" in body
    assert len(mails) == 1 and "주간 운영 요약" in mails[0][0]


def test_deep_health_fails_when_search_down(monkeypatch):
    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(main.urllib.request, "urlopen", down)
    r = client.get("/health/deep")
    assert r.status_code == 503 and r.json() == {"status": "fail", "search": False, "db": True}


def test_no_error_mail_outside_production(monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "alert", lambda s, b: sent.append(s))
    monkeypatch.setattr(settings, "app_env", "dev")
    assert ops.report_error("로컬", RuntimeError("x")) is False
    assert sent == [] and store.metrics_since("2000-01-01").get("error") == 1
