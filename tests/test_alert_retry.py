"""유력 후보가 생겨도 계속 찾기 + 알림 메일은 실제로 보냈을 때만 기록, 실패하면 재발송.

예전 문제(외부 리뷰·운영 확인 11/15): ① 유력 후보(candidate)가 되면 재매칭 대상에서 빠짐 — 운영 신고 44건 중 33건
② 메일 발송 결과를 안 보고 alert_sent를 올림 ③ 약한 후보는 보내기 전에 '알림 완료'로 적어 실패하면 다시 안 감
④ 실패한 메일을 다시 보내는 장치가 없음. 외부 서비스 없음: 검색(graph.match_stored)·알림(notifier)은 목킹.
"""

from datetime import datetime, timedelta, timezone

import pytest

from apps import store
from apps.agent import rematch


class Notifier:
    """ok=False면 메일 업체가 거절한 것처럼 False."""

    def __init__(self):
        self.ok = True
        self.sent: list[tuple[str, bool]] = []

    def notify(self, lost, matches, weak=False):
        self.sent.append((matches[0]["atc_id"], weak))
        return self.ok

    def verify(self, to, lang, link):
        return self.ok


@pytest.fixture(autouse=True)
def clean_db():
    store.init()
    store._run("DELETE FROM lost_items")
    store._run("DELETE FROM metrics")
    yield


@pytest.fixture
def mail(monkeypatch):
    n = Notifier()
    monkeypatch.setattr(rematch, "notifier", n)
    monkeypatch.setattr("apps.notifier.notifier", n)
    return n


def _report(**kw) -> str:
    lid = store.add("검정 지갑", {}, [], [], None, None, **kw)
    store._run("UPDATE lost_items SET email='me@x.com', email_verified=1 WHERE id=?", (lid,))
    return lid


def _search(monkeypatch, *items):
    """재매칭 검색 결과를 (atc_id, grade) 목록으로 고정."""
    monkeypatch.setattr(rematch.graph, "match_stored",
                        lambda *a, **k: [{"atc_id": a_, "fd_sn": "1", "name": "지갑", "grade": g} for a_, g in items])


def _metric(kind: str) -> int:
    return store.metrics_since("2000-01-01").get(kind, 0)


# ── 유력 후보가 생겨도 계속 찾기 ──

def test_candidate_report_keeps_searching_and_alerts_only_on_a_new_strong_match(monkeypatch, mail):
    lid = _report()
    _search(monkeypatch, ("A", 90))
    rematch.rematch_open(verbose=False)
    assert store.get(lid)["status"] == "candidate" and mail.sent == [("A", False)]

    rematch.rematch_open(verbose=False)                       # 같은 유력 후보 → 다시 알리지 않음(예전엔 아예 안 찾았음)
    assert mail.sent == [("A", False)]

    _search(monkeypatch, ("B", 92), ("A", 90))                # 다른 유력 후보가 새로 나옴 → 알림, 최고 후보 교체
    rematch.rematch_open(verbose=False)
    row = store.get(lid)
    assert mail.sent == [("A", False), ("B", False)] and row["best_match"]["atc_id"] == "B"
    assert row["status"] == "candidate" and row["weak_notified"] == ["A", "B"]


def test_candidate_does_not_get_weak_alerts(monkeypatch, mail):
    lid = _report(notify_weak=True)
    store._run("UPDATE lost_items SET status='candidate', best_match=? WHERE id=?",
               ('{"atc_id": "A", "grade": 90}', lid))
    _search(monkeypatch, ("C", 60))                           # 이미 유력 후보가 있으면 약한 후보는 참고 알림 안 함
    rematch.rematch_open(verbose=False)
    assert mail.sent == [] and store.get(lid)["status"] == "candidate"


def test_confirmed_report_is_not_rematched(monkeypatch, mail):
    lid = _report()
    store._run("UPDATE lost_items SET status='confirmed' WHERE id=?", (lid,))
    monkeypatch.setattr(rematch.graph, "match_stored", lambda *a, **k: pytest.fail("확인된 신고는 다시 찾지 않음"))
    rematch.rematch_open(verbose=False)


# ── 보냈을 때만 기록, 실패하면 재발송 ──

def test_failed_mail_is_not_counted_and_is_queued_for_retry(monkeypatch, mail):
    lid = _report(notify_weak=True)
    mail.ok = False
    _search(monkeypatch, ("W", 60))
    rematch.rematch_open(verbose=False)
    row = store.get(lid)
    assert _metric("alert_sent") == 0 and _metric("alert_failed") == 1
    assert row["weak_notified"] is None                        # 실패했으니 '알린 후보' 아님(예전엔 보내기 전에 적었음)
    p = row["alert_pending"]
    assert p["weak"] is True and p["tries"] == 1
    assert datetime.fromisoformat(p["next"]) - datetime.now(timezone.utc) > timedelta(minutes=14)


def test_retry_waits_for_its_time_then_sends_and_clears(monkeypatch, mail):
    lid = _report()
    mail.ok = False
    _search(monkeypatch, ("A", 90))
    rematch.rematch_open(verbose=False)
    mail.ok = True
    now = datetime.now(timezone.utc)
    assert rematch.retry_pending_alerts(now=now, verbose=False) == 0               # 아직 15분 안 됨
    assert rematch.retry_pending_alerts(now=now + timedelta(minutes=16), verbose=False) == 1
    row = store.get(lid)
    assert row["alert_pending"] is None and row["weak_notified"] == ["A"]
    assert _metric("alert_sent") == 1 and mail.sent[-1] == ("A", False)


def test_retry_backs_off_then_gives_up_and_tells_admin(monkeypatch, mail):
    lid = _report()
    mail.ok = False
    admin = []
    monkeypatch.setattr("apps.ops.alert", lambda subject, body="": admin.append(subject))
    _search(monkeypatch, ("A", 90))
    rematch.rematch_open(verbose=False)                        # 1차 실패 → 15분 뒤
    t = datetime.now(timezone.utc)
    gaps = []
    for _ in range(len(rematch.RETRY_MINUTES)):
        t = datetime.fromisoformat(store.get(lid)["alert_pending"]["next"]) + timedelta(seconds=1)
        rematch.retry_pending_alerts(now=t, verbose=False)
        p = store.get(lid)["alert_pending"]
        if p:
            gaps.append(round((datetime.fromisoformat(p["next"]) - t).total_seconds() / 60))
    assert gaps == [60, 360, 1440]                             # 15분 → 1시간 → 6시간 → 24시간
    assert store.get(lid)["alert_pending"] is None and _metric("alert_gaveup") == 1 and len(admin) == 1


def test_retry_drops_pending_when_report_no_longer_needs_mail(monkeypatch, mail):
    lid = _report()
    store.set_alert_pending(lid, {"weak": False, "tries": 1, "next": "2000-01-01T00:00:00+00:00"})
    store._run("UPDATE lost_items SET status='confirmed', matches='[{\"atc_id\": \"A\"}]' WHERE id=?", (lid,))
    assert rematch.retry_pending_alerts(verbose=False) == 0
    assert store.get(lid)["alert_pending"] is None and mail.sent == []


def test_mail_build_error_counts_as_failure(monkeypatch, mail):
    lid = _report()

    def broken(*a, **k):
        raise RuntimeError("translate down")

    monkeypatch.setattr(mail, "notify", broken)
    assert rematch.send_alert(store.get(lid), [{"atc_id": "A"}]) is False
    assert store.get(lid)["alert_pending"]["tries"] == 1


# ── 확인 메일 ──

def test_verification_mail_retries_in_place(monkeypatch):
    from apps import notifier as N
    monkeypatch.setattr(N.settings, "smtp_user", "u")
    monkeypatch.setattr(N.settings, "smtp_password", "p")
    monkeypatch.setattr(N.time, "sleep", lambda s: None)
    monkeypatch.setattr(N, "build_verify_email", lambda lang, link: ("s", "t", "h"))
    calls = []
    monkeypatch.setattr(N.EmailNotifier, "_send", staticmethod(lambda *a, **k: calls.append(1) or len(calls) == 3))
    assert N.EmailNotifier().verify("me@x.com", "ko", "http://x") is True and len(calls) == 3
    calls.clear()
    monkeypatch.setattr(N.EmailNotifier, "_send", staticmethod(lambda *a, **k: calls.append(1) and False))
    assert N.EmailNotifier().verify("me@x.com", "ko", "http://x") is False and len(calls) == 3


def test_alert_settings_say_so_when_verification_mail_fails(mail):
    from fastapi.testclient import TestClient

    from apps.api.main import app
    lid = store.add("검정 지갑", {}, [], [], None, None, token_hash=store.hash_token("tok"))
    mail.ok = False
    r = TestClient(app).post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": "tok"},
                             json={"email": "new@x.com", "watch_days": 30})
    assert r.status_code == 502 and "확인 메일" in r.json()["detail"]
    assert _metric("verify_mail_failed") == 1


def test_mail_logs_hide_addresses():
    from apps.notifier import _mask
    assert _mask("minsu@example.com") == "m•••@example.com" and _mask("") == "•••"
