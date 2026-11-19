"""MCP용 API(FIND-128) — 키가 있을 때만 열림, 신고를 저장하지 않음, 전화번호 등은 AI 전에 가림, 웹 결과와 같은 정보.
AI·검색 엔진·경찰 API는 목킹."""

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.api import mcp_api
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app)
KEY = "test-mcp-key"
H = {"X-MCP-Key": KEY}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    store.init()
    monkeypatch.setattr(settings, "mcp_api_keys", f"other-key, {KEY}")
    seen = []

    def fake_run(text, lost_date=None, lang="ko"):
        seen.append(text)
        return {"extracted": {"item": "지갑", "color": "검정", "place_context": "홍대입구역", "is_lost_report": True},
                "region_set": ["서울특별시 마포구"],
                "matches": [{"atc_id": f"F{i}", "fd_sn": "1", "name": "검정 지갑", "grade": 90 - i * 10,
                             "reason": "색·종류 일치", "link_place": "이어짐", "link_date": "맞음", "org_name": "서울마포경찰서",
                             "dep_place": "홍익지구대", "tel": "02-000-0000", "region": "서울특별시 마포구",
                             "region_en": "Mapo-gu, Seoul", "org_mt": "Seoul Mapo Police Station",
                             "image_url": "https://minwon24.police.go.kr/x.jpg", "description": "특이사항 : 카드 3장"}
                            for i in range(9)]}
    monkeypatch.setattr("apps.agent.graph.run", fake_run)
    return seen


def test_closed_without_keys_and_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "mcp_api_keys", "")
    assert client.post("/mcp-api/match", headers=H, json={"reports": [{"text": "검정 지갑"}]}).status_code == 404
    monkeypatch.setattr(settings, "mcp_api_keys", KEY)
    assert client.post("/mcp-api/match", headers={"X-MCP-Key": "nope"},
                       json={"reports": [{"text": "검정 지갑"}]}).status_code == 401


def test_match_returns_site_like_candidates_without_saving(setup):
    before = len(store.all_items())
    r = client.post("/mcp-api/match", headers=H,
                    json={"reports": [{"text": "어제 홍대입구역에서 검정 지갑 잃어버렸어요 010-1234-5678"}]})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    res = r.json()["results"][0]
    assert res["summary"] == "strong" and res["top_score"] == 90 and len(res["candidates"]) == 8   # 1건이면 8개
    c = res["candidates"][0]
    assert c["score"] == 90 and c["ai_reason"] == "색·종류 일치" and c["place_link"] == "이어짐"
    assert c["agency_en"] == "Seoul Mapo Police Station" and c["note"] == "카드 3장" and c["photo_url"]
    assert c["police_url"].endswith("pkupCmdtyMngId=F0&sortSn=1") and "naver.com" in c["naver_map"]
    assert len(store.all_items()) == before                                       # 저장 안 함
    assert "010-1234-5678" not in setup[0]                                        # 전화번호는 AI 전에 가림


def test_many_reports_default_three_each_and_limits():
    r = client.post("/mcp-api/match", headers=H, json={"reports": [{"text": f"검정 지갑 {i}"} for i in range(4)]})
    assert [len(x["candidates"]) for x in r.json()["results"]] == [3, 3, 3, 3]
    assert [x["input"] for x in r.json()["results"]] == [f"검정 지갑 {i}" for i in range(4)]   # 입력 순서 그대로
    too_many = {"reports": [{"text": "검정 지갑"}] * (mcp_api.MAX_REPORTS + 1)}
    assert client.post("/mcp-api/match", headers=H, json=too_many).status_code == 422
    assert client.post("/mcp-api/match", headers=H, json={"reports": [{"text": "x"}]}).status_code == 422


def test_item_status_reads_index_and_police(monkeypatch):
    monkeypatch.setattr(mcp_api, "_index_doc", lambda a, f: None)                 # 종결돼 색인에서 빠진 것
    monkeypatch.setattr(mcp_api, "_police_status", lambda a, f: {"status": "종결", "checked": True})
    r = client.get("/mcp-api/items/F2026111800003998", headers=H)
    d = r.json()
    assert r.status_code == 200 and d["in_our_index"] is False and d["police"]["status"] == "종결"
    assert d["item"]["police_url"].endswith("pkupCmdtyMngId=F2026111800003998&sortSn=1")
    assert client.get("/mcp-api/items/../../etc", headers=H).status_code in (400, 404)
    assert client.get("/mcp-api/items/X123", headers=H).status_code == 400
