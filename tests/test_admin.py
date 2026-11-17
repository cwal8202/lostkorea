"""운영 화면(/admin) — 비밀번호 없으면 404, 틀리면 401·여러 번이면 429, 응답 저장·색인 금지,
목록엔 신고 문장·이메일 없음, 줄을 열면 문장 + 연 기록, 외국어 신고는 처음 열 때 한 번만 번역해 저장,
기록 없는 숫자는 지어내지 않음. 검색 엔진·Gemini는 목킹."""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps import ops, store
from apps.api import admin, gemini
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app)
PW = "test-admin-pass"
KST = timezone(timedelta(hours=9))


def _auth(pw=PW):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def _api(pw=PW):
    return {**_auth(pw), "X-Admin": "1"}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    store.init()
    for t in ("lost_items", "metrics", "found_closures", "admin_views"):
        store._run(f"DELETE FROM {t}")
    monkeypatch.setattr(settings, "admin_password", PW)
    monkeypatch.setattr(ops, "index_status", lambda: {"count": 224043, "latest_found_at": datetime.now(KST).date().isoformat()})
    monkeypatch.setattr(admin, "_os", lambda path, body=None, timeout=5: {"count": 6327})
    monkeypatch.setattr(gemini, "_ACTIVE", {"model": None})
    yield


def _report(text="2호선에서 검정 지갑을 잃어버렸어요", email="", lang="ko", **kw):
    lid = store.add(text, {"item": "지갑", "category": "지갑", "color": "검정"}, ["서울 강남"], [], None, None,
                    email="", lang=lang, matches=kw.get("matches") or [])
    if email:
        store.set_pending_email(lid, email, None, None)
    return lid


# ── 보호 ──
def test_no_password_means_404(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "")
    for path in ("/admin", "/admin/data", "/admin/reports/" + "a" * 32):
        r = client.get(path, headers=_api())
        assert r.status_code == 404, path


def test_basic_auth_required_and_headers():
    r = client.get("/admin")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic ")
    assert client.get("/admin", headers=_auth("wrong")).status_code == 401
    r = client.get("/admin", headers=_auth())
    assert r.status_code == 200 and "운영 현황" in r.text
    assert "noindex" in r.headers["x-robots-tag"] and "no-store" in r.headers["cache-control"]
    assert r.headers["x-frame-options"] == "DENY"


def test_lockout_after_repeated_failures():
    for _ in range(settings.limit_admin_fail_hour):
        assert client.get("/admin", headers=_auth("wrong")).status_code == 401
    assert client.get("/admin", headers=_auth()).status_code == 429  # 맞는 비밀번호도 한 시간 막힘
    assert store.metrics_since("2000-01-01").get("admin_auth_fail") == settings.limit_admin_fail_hour


def test_json_needs_admin_header():
    assert client.get("/admin/data", headers=_auth()).status_code == 400  # 다른 사이트가 몰래 부르지 못하게


def test_robots_disallows_admin():
    assert "Disallow: /admin" in client.get("/robots.txt").text


# ── 목록 ──
def test_data_has_no_report_text_or_email():
    _report(text="I lost my black wallet near Hongdae, call [전화번호]", email="secret@example.com", lang="en")
    r = client.get("/admin/data", headers=_api())
    assert r.status_code == 200
    body = r.text
    assert "black wallet" not in body and "secret@example.com" not in body and "example.com" not in body
    d = r.json()
    row = d["ledger"][0]
    assert row["lang"] == "en" and row["cat"] == "지갑" and row["stages"] == [True, False, False, False, False]
    assert row["now"] == "찾는 중" and row["alert"] is False


def test_data_reads_real_records_only():
    today = datetime.now(KST).date()
    store.bump("view_ko", 5, day=(today - timedelta(days=1)).isoformat())
    store.bump("backup_kb", 469, day=(today - timedelta(days=3)).isoformat())
    d = client.get("/admin/data", headers=_api()).json()
    assert d["metrics"][(today - timedelta(days=1)).isoformat()]["view_ko"] == 5
    assert d["first_metric_day"] == (today - timedelta(days=3)).isoformat()  # 그 전 날짜는 '기록 없음'
    backup = next(c for c in d["checks"] if c["name"] == "DB 백업")
    assert backup["level"] == "warn" and "마지막 DB 백업" in backup["title"]  # 3일 전이 마지막 → 이상
    assert d["checks"][0]["level"] == "warn"  # 이상 항목이 위로
    assert d["now"]["index"]["count"] == 224043 and d["now"]["mail"]["n"] == 0


def test_search_engine_down_is_critical(monkeypatch):
    def boom():
        raise OSError("refused")
    monkeypatch.setattr(ops, "index_status", boom)
    d = client.get("/admin/data", headers=_api()).json()
    assert d["checks"][0]["level"] == "crit" and d["now"]["index"] is None


def test_pending_alert_and_fallback_model_flagged(monkeypatch):
    lid = _report()
    store.set_alert_pending(lid, {"weak": False, "tries": 2, "next": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(gemini, "_ACTIVE", {"model": "gemini-3.8-flash"})
    names = {c["name"]: c for c in client.get("/admin/data", headers=_api()).json()["checks"]}
    assert names["알림 메일"]["level"] == "warn" and "1건" in names["알림 메일"]["title"]
    assert names["AI 모델"]["level"] == "warn" and "3.8" in names["AI 모델"]["title"]


# ── 대장 단계 ──
def test_stages_and_now_label():
    row = {"status": "confirmed", "best_grade": 85, "matches": [],
           "outcome": {"stage": "found", "history": [{"stage": "contacted"}, {"stage": "found"}]}}
    assert admin.stages(row) == [True, True, True, True, True] and admin.now_label(row) == "찾았어요"
    row = {"status": "open", "best_grade": 60, "matches": [{"grade": 60}],
           "outcome": {"stage": "not_mine", "history": [{"stage": "not_mine"}]}}
    assert admin.stages(row) == [True, False, True, False, False] and admin.now_label(row) == "내 물건이 아니었어요"
    assert admin.now_label({"status": "expired"}) == "기간 끝(만료)"


@pytest.mark.parametrize("text,ui,lang", [
    ("2호선에서 Louis Vuitton 지갑 잃어버림", "ko", "ko"),
    ("I lost my AirPods Pro at Gangnam station", "ko", "en"),
    ("I lost my phone at 홍대입구역", "en", "en"),
    ("에어팟 프로 분실", "en", "ko"),
    ("江南站で黒い財布をなくしました", "en", "ja"),
    ("我在江南站丢了黑色钱包", "en", "zh"),
    ("Tôi làm mất ví ở ga Gangnam", "en", "vi"),
    ("Call me [전화번호] wallet lost", "ko", "en"),  # 가림 표시의 한글은 언어 판단에서 뺌
])
def test_text_lang(text, ui, lang):
    assert admin.text_lang(text, ui) == lang


def test_image_report_detected():
    ex = {"color": "검정", "brand": "", "item": "지갑", "features": "카드 2장"}
    assert admin.is_image_report({"text": "검정 지갑 / 카드 2장 / 메모", "extracted": ex})
    assert not admin.is_image_report({"text": "검정 지갑을 잃어버렸어요", "extracted": ex})


# ── 한 건 열기 ──
def test_detail_shows_text_logs_view_and_hides_email():
    lid = _report(email="secret@example.com", matches=[{"atc_id": "F1", "fd_sn": "1", "name": "검정 지갑", "grade": 82,
                                                         "org_name": "강남경찰서", "found_at": "2026-11-10", "reason": "색 일치"}])
    r = client.get(f"/admin/reports/{lid}", headers=_api())
    d = r.json()
    assert r.status_code == 200 and "검정 지갑을" in d["text"] and "secret" not in r.text
    assert dict(d["info"])["알림 이메일"] == "입력했지만 아직 확인 안 함"
    assert d["cands"][0]["grade"] == 82 and d["views"] == []
    assert len(client.get(f"/admin/reports/{lid}", headers=_api()).json()["views"]) == 1  # 앞서 연 기록
    assert client.get("/admin/reports/" + "0" * 32, headers=_api()).status_code == 404


def test_translation_once_then_saved(monkeypatch):
    calls = []

    def fake(prompt, timeout=60):
        calls.append(prompt)
        return {"ko": "강남역에서 에어팟을 잃어버렸어요"}
    monkeypatch.setattr(gemini, "generate_json", fake)
    lid = _report(text="I lost my AirPods at Gangnam station", lang="en")
    assert client.get(f"/admin/reports/{lid}", headers=_api()).json()["needs_ko"] is True
    for _ in range(2):
        r = client.post(f"/admin/reports/{lid}/ko", headers=_api())
        assert r.status_code == 200 and r.json()["text_ko"].startswith("강남역")
    assert len(calls) == 1 and store.get(lid)["text_ko"]  # 두 번째부터는 저장된 번역
    ko = _report()
    assert client.post(f"/admin/reports/{ko}/ko", headers=_api()).json()["text_ko"] is None and len(calls) == 1


def test_translation_failure_is_502(monkeypatch):
    monkeypatch.setattr(gemini, "generate_json", lambda prompt, timeout=60: None)
    lid = _report(text="I lost my wallet", lang="en")
    assert client.post(f"/admin/reports/{lid}/ko", headers=_api()).status_code == 502
    assert not store.get(lid).get("text_ko")


def test_admin_views_pruned_after_a_year():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    store._run("INSERT INTO admin_views (at, report_key, action) VALUES (?, ?, ?)", (old, "x", "open"))
    store.log_admin_view("abc")
    rows = store._rows("SELECT report_key FROM admin_views")
    assert [r["report_key"] for r in rows] != ["x"] and all(r["report_key"] != "x" for r in rows)


def test_page_is_valid_json_contract():
    """화면 JS가 쓰는 키가 다 있는지(이름이 바뀌면 화면이 조용히 빈칸이 됨)."""
    d = client.get("/admin/data", headers=_api()).json()
    for k in ("read_at", "today", "checks", "metrics", "first_metric_day", "closures", "first_closure_day",
              "our_closed", "ledger", "now", "ai_price", "grade"):
        assert k in d, k
    page = client.get("/admin", headers=_auth()).text
    for k in ("D.our_closed", "D.first_closure_day", "D.ai_price", "D.grade", "D.now", "D.checks"):
        assert k in page or k.split(".")[1] in page, k
    json.dumps(d)
