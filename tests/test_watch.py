"""알림 기간(감시 만료) + 결과창 '알림 설정' + 이메일 확인(double opt-in).

- 기간이 지난 open 신고는 재매칭하지 않고 expired로(LLM 비용 중단).
- watch_until 없는 기존 신고는 등록일 + 기본 기간으로 간주.
- POST /lost-items/{id}/alerts: 관리 토큰 필수, 새 이메일이면 **확인 메일만**(결과는 확인 후), 만료 신고 재개.
- POST /email/verify: 확인 → 알림 활성화, 서버가 저장한 후보가 기준을 넘으면 현재 결과 발송.
외부 서비스 없음: 검색·알림 목킹.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.agent import rematch
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app)


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []      # 결과·알림 메일 수신자
        self.verify_links: list[str] = []

    def notify(self, lost, matches, weak=False):
        self.sent.append(lost.get("email"))

    def verify(self, to, lang, link):
        self.verify_links.append(link)


@pytest.fixture(autouse=True)
def clean_db():
    store.init()
    store._run("DELETE FROM lost_items")
    yield


@pytest.fixture
def fake(monkeypatch):
    n = FakeNotifier()
    monkeypatch.setattr(rematch, "notifier", n)
    monkeypatch.setattr("apps.notifier.notifier", n)
    return n


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _report(matches=None, **kw):
    token = store.new_token()
    lid = store.add("검정 지갑", {}, [], [], None, None, token_hash=store.hash_token(token),
                    matches=matches or [], **kw)
    return lid, token


def _verified(lid, email="me@x.com"):
    store._run("UPDATE lost_items SET email=?, email_verified=1 WHERE id=?", (email, lid))


def test_watch_until_after_clamps():
    d = lambda s: (datetime.fromisoformat(s) - datetime.now(timezone.utc)).days  # noqa: E731
    assert d(rematch.watch_until_after(7)) in (6, 7)
    assert d(rematch.watch_until_after(9999)) in (settings.watch_days_max - 1, settings.watch_days_max)
    assert d(rematch.watch_until_after(None)) in (settings.watch_days_default - 1, settings.watch_days_default)


def test_expired_report_is_not_rematched(monkeypatch, fake):
    lid, _ = _report(watch_until=_iso(-1))
    called = []
    monkeypatch.setattr(rematch.graph, "match_stored", lambda *a, **k: called.append(1) or [])
    rematch.rematch_open(verbose=False)
    assert called == []                          # 검색·LLM 호출 없음
    assert store.get(lid)["status"] == "expired"


def test_active_verified_report_is_rematched_and_alerted(monkeypatch, fake):
    lid, _ = _report(watch_until=_iso(10))
    _verified(lid)
    monkeypatch.setattr(rematch.graph, "match_stored",
                        lambda *a, **k: [{"atc_id": "A_1", "grade": 90, "score": 0.9}])
    rematch.rematch_open(verbose=False)
    row = store.get(lid)
    assert row["status"] == "candidate" and fake.sent == ["me@x.com"]
    assert row["matches"][0]["atc_id"] == "A_1"   # 메일·확인용 후보도 최신으로 저장


def test_unverified_email_gets_no_alert(monkeypatch, fake):
    lid, _ = _report(watch_until=_iso(10))
    store._run("UPDATE lost_items SET email='me@x.com' WHERE id=?", (lid,))   # 확인 안 됨
    monkeypatch.setattr(rematch.graph, "match_stored",
                        lambda *a, **k: [{"atc_id": "A_1", "grade": 90, "score": 0.9}])
    rematch.rematch_open(verbose=False)
    assert store.get(lid)["status"] == "candidate" and fake.sent == []


def test_rematch_continues_after_one_failure(monkeypatch, fake):
    a, _ = _report(watch_until=_iso(10))
    b, _ = _report(watch_until=_iso(10))
    _verified(b)
    calls = []

    def flaky(*args, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("gemini down")
        return [{"atc_id": "A_1", "grade": 90, "score": 0.9}]
    monkeypatch.setattr(rematch.graph, "match_stored", flaky)
    hits = rematch.rematch_open(verbose=False)
    assert len(calls) == 2 and len(hits) == 1     # 첫 건 실패해도 다음 건 처리


def test_legacy_report_without_watch_until_uses_created_at(monkeypatch, fake):
    lid, _ = _report()
    old = (datetime.now(timezone.utc) - timedelta(days=settings.watch_days_default + 1)).isoformat()
    store._run("UPDATE lost_items SET created_at=? WHERE id=?", (old, lid))
    monkeypatch.setattr(rematch.graph, "match_stored", lambda *a, **k: [])
    rematch.rematch_open(verbose=False)
    assert store.get(lid)["status"] == "expired"


def test_alerts_new_email_sends_verification_only(fake):
    lid, token = _report(matches=[{"atc_id": "A_1", "name": "지갑", "grade": 90}])
    r = client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token},
                    json={"email": "me@x.com", "notify_weak": True, "watch_days": 90})
    body = r.json()
    assert r.status_code == 200 and body["verification_sent"] and not body["email_verified"]
    assert fake.sent == [] and len(fake.verify_links) == 1   # 결과 메일은 확인 전엔 안 감
    assert "/email/verify?token=" in fake.verify_links[0]
    row = store.get(lid)
    assert row["email"] == "me@x.com" and not row["email_verified"] and row["notify_weak"]


def test_verify_turns_on_alerts_and_sends_stored_results(fake):
    lid, token = _report(matches=[{"atc_id": "A_1", "name": "지갑", "grade": 90}])
    client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token},
                json={"email": "me@x.com", "watch_days": 30})
    vtoken = fake.verify_links[0].split("token=")[1]
    assert client.get(f"/email/verify?token={vtoken}").status_code == 200          # GET은 상태 안 바꿈
    assert not store.get(lid)["email_verified"]
    r = client.post("/email/verify", json={"token": vtoken})
    assert r.status_code == 200 and r.json()["sent_results"]
    assert store.get(lid)["email_verified"] and fake.sent == ["me@x.com"]
    assert client.post("/email/verify", json={"token": vtoken}).status_code == 400  # 1회용


def test_verify_rejects_bad_or_expired_token(fake):
    lid, token = _report()
    client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token}, json={"email": "me@x.com"})
    vtoken = fake.verify_links[0].split("token=")[1]
    store._run("UPDATE lost_items SET verify_expires=? WHERE id=?", (_iso(-1), lid))
    assert client.post("/email/verify", json={"token": vtoken}).status_code == 400
    assert client.post("/email/verify", json={"token": "nope"}).status_code == 400


def test_verification_mail_per_address_limit(monkeypatch, fake):
    monkeypatch.setattr(settings, "limit_verify_mail_day", 1)
    a, ta = _report()
    b, tb = _report()
    assert client.post(f"/lost-items/{a}/alerts", headers={"X-Report-Token": ta}, json={"email": "v@x.com"}).status_code == 200
    r = client.post(f"/lost-items/{b}/alerts", headers={"X-Report-Token": tb}, json={"email": "V@x.com"})
    assert r.status_code == 429 and len(fake.verify_links) == 1   # 같은 주소(대소문자 무관) 폭탄 방지


def test_alerts_reopens_expired_and_ignores_weak_without_email(fake):
    lid, token = _report(status="expired")
    r = client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token},
                    json={"email": "", "notify_weak": True, "watch_days": 30})
    assert r.json()["status"] == "open" and r.json()["notify_weak"] is False
    assert store.get(lid)["status"] == "open"


def test_alerts_requires_token_and_valid_email(fake):
    lid, token = _report()
    assert client.post(f"/lost-items/{lid}/alerts", json={"email": "me@x.com"}).status_code == 404
    assert client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token},
                       json={"email": "nope"}).status_code == 400
    assert client.post("/lost-items/missing/alerts", headers={"X-Report-Token": token}, json={}).status_code == 404


# ── 알림 메일 링크로 결과 다시 보기(다른 기기에서도) ──

def test_email_link_token_opens_report_on_another_device():
    lid, token = _report(matches=[{"atc_id": "A_1", "name": "지갑", "grade": 90}], email="me@x.com")
    link = store.new_link_token(lid)
    assert link != token
    r = client.get(f"/lost-items/{lid}", headers={"X-Report-Token": link})      # 등록한 브라우저가 아니어도
    body = r.json()
    assert r.status_code == 200 and body["matches"][0]["atc_id"] == "A_1"
    assert "email" not in body and body["email_masked"] == "m•••@x.com"          # 이메일은 가린 형태로만
    assert "link_hashes" not in body and "token_hash" not in body
    ok = client.post(f"/lost-items/{lid}/confirm", headers={"X-Report-Token": link}, json={"atc_id": "A_1"})
    assert ok.status_code == 200 and store.get(lid)["status"] == "confirmed"      # '내 물건이에요'까지 가능


def test_email_link_tokens_keep_recent_ones_and_reject_others():
    lid, _ = _report()
    first = store.new_link_token(lid)
    second = store.new_link_token(lid)
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": first}).status_code == 200  # 지난 메일 링크도 유효
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": second}).status_code == 200
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": "guess"}).status_code == 404
    other, _ = _report()
    assert client.get(f"/lost-items/{other}", headers={"X-Report-Token": second}).status_code == 404  # 다른 신고엔 무효
    for _ in range(store.LINK_KEEP):
        store.new_link_token(lid)
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": first}).status_code == 404  # 오래된 링크부터 만료
    assert len(store.get(lid)["link_hashes"]) == store.LINK_KEEP


def test_alerts_without_email_field_keep_current_email(fake):
    lid, token = _report(email="me@x.com")
    store._run("UPDATE lost_items SET email_verified=1 WHERE id=?", (lid,))
    r = client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token}, json={"watch_days": 90})
    assert r.status_code == 200 and r.json()["email"] == "m•••@x.com" and r.json()["email_verified"]
    assert store.get(lid)["email"] == "me@x.com" and fake.verify_links == []      # 기간만 바꿔도 알림 유지
    client.post(f"/lost-items/{lid}/alerts", headers={"X-Report-Token": token}, json={"email": ""})
    assert store.get(lid)["email"] == ""                                           # ""는 알림 끄기


# ── 알림 그만 받기(수신 거부) + 보내는 주소 ──

def test_unsubscribe_turns_off_alerts_only_with_unsub_token():
    lid, token = _report(email="me@x.com")
    store._run("UPDATE lost_items SET email_verified=1 WHERE id=?", (lid,))
    u = store.new_unsub_token(lid)
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": u}).status_code == 404  # 신고를 열 권한은 없음
    assert "Stop alerts" in client.get("/unsubscribe", params={"r": f"{lid}.{u}"}).text
    assert store.get(lid)["email"] == "me@x.com"                               # GET은 아무것도 안 바꿈(스캐너 대비)
    r = client.post("/unsubscribe", params={"r": f"{lid}.wrong"}, data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and store.get(lid)["email"] == "me@x.com"      # 틀린 토큰 — 같은 화면, 변화 없음
    r = client.post("/unsubscribe", params={"r": f"{lid}.{u}"}, data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and store.get(lid)["email"] == ""              # 원클릭으로 알림 꺼짐
    link = store.new_link_token(lid)                                          # 수신 거부 토큰이 결과 링크 토큰을 밀어내지 않음
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": link}).status_code == 200


def test_alert_email_has_sender_reply_to_and_one_click_unsubscribe(monkeypatch):
    import smtplib

    from apps import notifier as N
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for k, v in (("smtp_user", "acct"), ("smtp_password", "pw"), ("smtp_from", "alerts@lostkorea.com"),
                 ("smtp_reply_to", "help@lostkorea.com")):
        monkeypatch.setattr(N.settings, k, v)
    monkeypatch.setattr(N.gemini, "translate_strings", lambda *a: {})
    lid, _ = _report(email="me@x.com")
    lost = store.get(lid)
    N.EmailNotifier().notify(lost, [{"atc_id": "A_1", "name": "지갑", "color": "검정", "grade": 90}])
    m = sent[0]
    assert "alerts@lostkorea.com" in m["From"] and m["Reply-To"] == "help@lostkorea.com"
    assert m["List-Unsubscribe"].startswith("<") and "/unsubscribe?r=" + lid in m["List-Unsubscribe"]
    assert m["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    html = m.get_body(("html",)).get_content()
    assert "/unsubscribe?r=" in html and "알림 그만 받기" in html
