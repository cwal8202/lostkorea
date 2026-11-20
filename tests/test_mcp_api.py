"""MCP(FIND-128) — 기관 키(운영 화면 발급·회수, DB엔 해시만), 키 없이 하루 3건, 신고 저장 안 함, 전화번호 가림,
원격 MCP(/mcp)·내 PC용 주소(/mcp-api), 운영자 쿠키로 사이트 한도 없이. AI·검색 엔진·경찰 API는 목킹."""

import base64

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.api import admin, mcp_api
from apps.api.config import settings
from apps.api.main import app

client = TestClient(app)
PW = "test-admin-pass"
MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "MCP-Protocol-Version": "2025-06-18"}


def _match(i: int) -> dict:
    return {"atc_id": f"F{i}", "fd_sn": "1", "name": "검정 지갑", "grade": 90 - i * 10, "reason": "색·종류 일치", "score": 0.5,
            "link_place": "이어짐", "link_date": "맞음", "org_name": "서울마포경찰서", "dep_place": "홍익지구대",
            "tel": "02-000-0000", "region": "서울특별시 마포구", "region_en": "Mapo-gu, Seoul",
            "org_mt": "Seoul Mapo Police Station", "image_url": "https://minwon24.police.go.kr/x.jpg",
            "description": "특이사항 : 카드 3장"}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    store.init()
    for t in ("mcp_keys", "mcp_usage", "lost_items"):
        store._run(f"DELETE FROM {t}")
    monkeypatch.setattr(settings, "admin_password", PW)
    seen = []

    def fake_run(text, lost_date=None, lang="ko"):
        seen.append(text)
        return {"extracted": {"item": "지갑", "color": "검정", "place_context": "홍대입구역", "is_lost_report": True},
                "region_set": ["서울특별시 마포구"], "queries": ["검정 지갑"], "matches": [_match(i) for i in range(9)]}
    monkeypatch.setattr("apps.agent.graph.run", fake_run)
    monkeypatch.setattr(mcp_api, "_index_doc", lambda a, f: None)
    monkeypatch.setattr(mcp_api, "_police_status", lambda a, f: {"status": "보관중", "checked": True})
    return seen


def _key(limit=300, name="시험 기관"):
    return store.mcp_key_create(name, limit)


# ── 내 PC용 MCP가 부르는 /mcp-api — 키 필수 ──
def test_mcp_api_needs_a_valid_key():
    body = {"reports": [{"text": "black wallet"}]}
    assert client.post("/mcp-api/match", json=body).status_code == 401
    assert client.post("/mcp-api/match", headers={"X-MCP-Key": "lk_wrong"}, json=body).status_code == 401
    kid, key = _key()
    store.mcp_key_revoke(kid)
    assert client.post("/mcp-api/match", headers={"X-MCP-Key": key}, json=body).status_code == 401   # 회수한 키


def test_match_returns_site_like_candidates_without_saving_and_counts_usage(setup):
    kid, key = _key()
    r = client.post("/mcp-api/match", headers={"Authorization": f"Bearer {key}"},
                    json={"reports": [{"text": "어제 홍대입구역에서 검정 지갑 잃어버렸어요 010-1234-5678"}]})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    res = r.json()["results"][0]
    assert res["summary"] == "strong" and res["top_score"] == 90 and len(res["candidates"]) == 8   # 1건이면 8개
    c = res["candidates"][0]
    assert c["ai_reason"] == "색·종류 일치" and c["agency_en"] == "Seoul Mapo Police Station" and c["note"] == "카드 3장"
    assert c["police_url"].endswith("pkupCmdtyMngId=F0&sortSn=1") and "naver.com" in c["naver_map"]
    assert store.all_items() == [] and "010-1234-5678" not in setup[0]            # 저장 안 함·전화번호 가림
    assert store.mcp_usage_today(kid) == 1


def test_key_daily_limit_and_unlimited_operator_key():
    _, key = _key(limit=2)
    body = {"reports": [{"text": f"검정 지갑 {i}"} for i in range(3)]}
    r = client.post("/mcp-api/match", headers={"X-MCP-Key": key}, json=body)
    assert r.status_code == 429                                                  # 넘으면 하나도 안 돌림
    _, op = _key(limit=0, name="운영자")
    for _ in range(3):
        assert client.post("/mcp-api/match", headers={"X-MCP-Key": op}, json=body).status_code == 200
    too_many = {"reports": [{"text": "검정 지갑"}] * (mcp_api.MAX_REPORTS + 1)}
    assert client.post("/mcp-api/match", headers={"X-MCP-Key": op}, json=too_many).status_code == 422


def test_item_status_with_key():
    _, key = _key()
    d = client.get("/mcp-api/items/F2026111800003998", headers={"X-MCP-Key": key}).json()
    assert d["in_our_index"] is False and d["police"]["status"] == "보관중"
    assert client.get("/mcp-api/items/X123", headers={"X-MCP-Key": key}).status_code == 400


# ── 원격 MCP /mcp — 주소만 붙여 넣으면 됨 ──
def _rpc(c, method, params=None, headers=None, rid=1):
    r = c.post("/mcp", headers={**MCP_H, **(headers or {})},
               json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def _call(c, name, args, headers=None):
    import json as _json

    res = _rpc(c, "tools/call", {"name": name, "arguments": args}, headers)["result"]
    out = res.get("structuredContent") or res["content"][0]["text"]
    if isinstance(out, str) and not res.get("isError"):
        out = _json.loads(out)
    return res.get("isError", False), out


def test_remote_mcp_lists_tools_and_limits_public_to_three_a_day():
    with TestClient(app) as c:
        init = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})
        assert init["result"]["serverInfo"]["name"] == "LostKorea"
        names = [t["name"] for t in _rpc(c, "tools/list")["result"]["tools"]]
        assert names == ["find_matches", "search_found_items", "get_found_item"]
        err, out = _call(c, "find_matches", {"reports": [{"text": "검정 지갑"}] * 4})
        assert err and "3건" in str(out)                                          # 키 없이 한 번에 3건까지
        err, out = _call(c, "find_matches", {"reports": [{"text": "검정 지갑"}] * 3})
        assert not err and len(out["results"]) == 3 and len(out["results"][0]["candidates"]) == 3
        err, out = _call(c, "find_matches", {"reports": [{"text": "검정 지갑"}]})
        assert err and "하루" in str(out)                                         # 하루 3건을 다 씀
        err, out = _call(c, "get_found_item", {"atc_id": "F2026111800003998"})
        assert not err and out["police"]["status"] == "보관중"


def test_remote_mcp_with_key_allows_more_and_rejects_bad_key():
    _, key = _key()
    with TestClient(app) as c:
        err, out = _call(c, "find_matches", {"reports": [{"text": "검정 지갑"}] * 5}, {"X-MCP-Key": key})
        assert not err and len(out["results"]) == 5
        err, out = _call(c, "find_matches", {"reports": [{"text": "검정 지갑"}]}, {"X-MCP-Key": "lk_nope"})
        assert err and "키" in str(out)


# ── 운영자: 쿠키로 사이트 한도 없이 + 키 발급·회수 ──
def _auth():
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{PW}".encode()).decode()}


def test_admin_cookie_lifts_site_limits_and_marks_reports(monkeypatch):
    c = TestClient(app)
    r = c.get("/admin", headers=_auth())
    assert r.status_code == 200 and admin.ADMIN_COOKIE in r.cookies
    for _ in range(settings.limit_report_10min + 2):                              # 10분 5건 한도를 넘어도
        assert c.post("/lost-items", json={"text": "검정 지갑을 잃어버렸어요"}).status_code == 200
    assert {x["user_id"] for x in store.all_items()} == {"admin"}                # '운영자 시험' 표시
    fresh = TestClient(app)                                                     # 쿠키 없는 사람은 한도 그대로
    codes = [fresh.post("/lost-items", json={"text": "검정 지갑을 잃어버렸어요"}).status_code
             for _ in range(settings.limit_report_10min + 1)]
    assert codes[-1] == 429
    c.cookies.set(admin.ADMIN_COOKIE, "9999999999.forged")
    assert c.post("/lost-items", json={"text": "검정 지갑을 잃어버렸어요"}).status_code in (200, 429)
    assert not admin.is_admin_request(type("R", (), {"cookies": {admin.ADMIN_COOKIE: "9999999999.forged"}})())


def test_admin_issues_lists_and_revokes_keys():
    h = {**_auth(), "X-Admin": "1"}
    r = client.post("/admin/mcp-keys", headers=h, json={"name": "서울역 유실물센터", "daily_limit": 300})
    d = r.json()
    assert r.status_code == 200 and d["key"].startswith("lk_")
    keys = client.get("/admin/mcp-keys", headers=h).json()["keys"]
    assert keys[0]["name"] == "서울역 유실물센터" and d["key"] not in str(keys)       # 목록엔 키 원문 없음
    assert client.post(f"/admin/mcp-keys/{d['id']}/revoke", headers=h).json()["ok"]
    assert store.mcp_key_find(d["key"]) is None
    assert client.post("/admin/mcp-keys", headers=h, json={"name": "", "daily_limit": 300}).status_code == 400
    assert client.post("/admin/mcp-keys", json={"name": "x"}).status_code == 401
