"""작은 수정 묶음(11/15) — 외부 리뷰 지적 중 바로 고칠 수 있는 것.

신고 문장 개인정보 가림 / 사진 대조 다운로드는 경찰청 사진 주소만 / 확인·제외는 관리번호+순번 / 판정한 후보 12개 저장.
외부 서비스 없음.
"""

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.agent import graph, rematch
from apps.api import main, pii
from apps.api.main import app

client = TestClient(app)


@pytest.mark.parametrize("raw, want", [
    ("지갑 잃어버림 010-1234-5678로 연락주세요", "지갑 잃어버림 [전화번호]로 연락주세요"),
    ("연락 01012345678", "연락 [전화번호]"),
    ("+82 10 1234 5678", "[전화번호]"),
    ("카드 1234-5678-9012-3456 들어있음", "카드 [카드번호] 들어있음"),
    ("주민번호 900101-1234567", "주민번호 [주민번호]"),
    ("passport M12345678 lost", "passport [여권번호] lost"),
    ("email me a@b.com", "email me [이메일]"),
])
def test_personal_numbers_are_masked(raw, want):
    assert pii.mask(raw)[0] == want


@pytest.mark.parametrize("keep", ["iPhone A2848 검정, 2026-11-14 오후 3시", "현금 50000원, 2호선 강남역",
                                  "신한카드 2장, 버스 146번", "2026.11.14 15:30 분실"])
def test_ordinary_details_are_not_masked(keep):
    assert pii.mask(keep) == (keep, 0)


def test_report_text_is_masked_before_ai_and_storage(monkeypatch):
    seen = []

    def run(text, lost_date=None, lang="ko"):
        seen.append(text)
        return {"extracted": {"is_lost_report": True}, "matches": []}

    monkeypatch.setattr("apps.agent.graph.run", run)
    r = client.post("/lost-items", json={"text": "검정 지갑, 찾으면 010-9876-5432로 연락 주세요"})
    assert r.status_code == 200 and seen == ["검정 지갑, 찾으면 [전화번호]로 연락 주세요"]
    assert "9876" not in store.get(r.json()["id"])["text"]


def test_photo_compare_only_downloads_police_photos(monkeypatch):
    monkeypatch.setattr(graph.urllib.request, "urlopen", lambda *a, **k: pytest.fail("허용 안 된 주소로 요청하면 안 됨"))
    assert graph._fetch_image_b64("http://169.254.169.254/latest/meta-data") == (None, "")
    assert graph._fetch_image_b64("https://evil.example.com/x.jpg") == (None, "")


def test_confirm_and_dismiss_use_serial_number_too():
    store.init()
    a1 = {"atc_id": "A", "fd_sn": "1", "name": "가방", "grade": 70}
    a2 = {"atc_id": "A", "fd_sn": "2", "name": "가방 안 지갑", "grade": 90}       # 같은 관리번호, 다른 순번
    lid = store.add("지갑", {}, [], [], a2, 90, status="candidate", token_hash=store.hash_token("t"), matches=[a1, a2])
    h = {"X-Report-Token": "t"}
    assert client.post(f"/lost-items/{lid}/dismiss", headers=h, json={"atc_id": "A", "fd_sn": "1"}).status_code == 200
    row = store.get(lid)
    assert row["dismissed"] == ["A_1"]
    assert not rematch.is_dismissed(a2, set(row["dismissed"])) and rematch.is_dismissed(a1, set(row["dismissed"]))
    assert client.post(f"/lost-items/{lid}/confirm", headers=h, json={"atc_id": "A", "fd_sn": "2"}).status_code == 200
    assert store.get(lid)["best_match"]["fd_sn"] == "2"                            # 예전엔 관리번호만 봐 첫 번째가 잡힘
    assert rematch.is_dismissed({"atc_id": "OLD"}, {"OLD"})                        # 옛 기록(관리번호만)도 그대로


def test_all_graded_candidates_are_kept():
    ms = [{"atc_id": str(i), "grade": 50} for i in range(15)]
    assert len(rematch.slim_matches(ms)) == 12                                     # 예전 6 → 다시 열어도 12개
