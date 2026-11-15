"""수령 진행(성과) 버튼 — '내 물건이에요' 뒤 연락했어요·방문했어요·찾았어요·내 물건이 아니었어요.

심사·운영에서 '실제로 찾았는가'를 재는 유일한 장치. 신고를 지워도 익명 기록(단계·날짜·언어·물품 분류)은 남음.
외부 서비스 없음.
"""

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.api.main import app

client = TestClient(app)
MATCH = {"atc_id": "F1", "fd_sn": "1", "name": "검정 지갑", "category": "지갑 > 남성용 지갑", "source": "police", "grade": 88}


@pytest.fixture(autouse=True)
def clean():
    store.init()
    store._run("DELETE FROM lost_items")
    store._run("DELETE FROM metrics")
    store._run("DELETE FROM outcomes")
    yield


def _report(status="confirmed") -> str:
    lid = store.add("검정 지갑", {}, [], [], MATCH, 88, status=status, lang="ja",
                    token_hash=store.hash_token("tok"), matches=[MATCH])
    return lid


def _post(lid, stage, token="tok"):
    return client.post(f"/lost-items/{lid}/outcome", headers={"X-Report-Token": token}, json={"stage": stage})


def test_found_is_recorded_on_report_and_anonymously():
    lid = _report()
    r = _post(lid, "found")
    assert r.status_code == 200 and r.json()["stage"] == "found" and r.json()["status"] == "confirmed"
    assert store.get(lid)["outcome"]["stage"] == "found" and store.get(lid)["outcome"]["atc_id"] == "F1"
    (o,) = store._rows("SELECT * FROM outcomes")
    assert o["stage"] == "found" and o["lang"] == "ja" and o["item_category"] == "지갑" and o["item_grade"] == 88
    assert lid not in o["report_key"] and o["days_from_report"] == 0          # 신고 id는 해시로만
    assert store.metrics_since("2000-01-01").get("outcome_found") == 1


def test_same_button_twice_counts_once_and_steps_accumulate():
    lid = _report()
    for stage in ("contacted", "contacted", "visited", "found"):
        assert _post(lid, stage).status_code == 200
    m = store.metrics_since("2000-01-01")
    assert (m.get("outcome_contacted"), m.get("outcome_visited"), m.get("outcome_found")) == (1, 1, 1)
    assert [h["stage"] for h in store.get(lid)["outcome"]["history"]] == ["contacted", "visited", "found"]


def test_not_mine_drops_the_candidate_and_searches_again():
    lid = _report()
    r = _post(lid, "not_mine")
    row = store.get(lid)
    assert r.json()["status"] == "open" and row["status"] == "open" and row["dismissed"] == ["F1"]
    assert row["outcome"]["stage"] == "not_mine"


def test_only_after_confirm_and_only_known_steps_and_owner():
    assert _post(_report(status="candidate"), "found").status_code == 400     # '내 물건이에요' 전엔 안 됨
    lid = _report()
    assert _post(lid, "bought_new_one").status_code == 422
    assert _post(lid, "found", token="wrong").status_code in (403, 404)


def test_anonymous_record_survives_report_deletion():
    lid = _report()
    _post(lid, "found")
    assert client.delete(f"/lost-items/{lid}", headers={"X-Report-Token": "tok"}).status_code == 200
    assert store.get(lid) is None and store.outcome_counts_since("2000-01-01") == {"found": 1}


def test_my_reports_show_outcome_step():
    lid = _report()
    _post(lid, "visited")
    r = client.post("/lost-items/mine", json={"items": [{"id": lid, "token": "tok"}]})
    assert r.json()[0]["outcome"] == "visited"
