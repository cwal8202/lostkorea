"""검색어 만들기 — 기본은 브랜드·색·물건·특징 한 문장, 예전 방식(fanout)은 넓게~좁게 여러 검색어(되돌리기용)."""

from apps.agent import graph
from apps.agent.fanout import build_queries, fanout_queries, full_query

EX = {"brand": "닥스", "color": "검정", "item": "지갑", "category": "지갑", "features": "카드 2장"}


def test_default_is_one_full_query():
    assert build_queries(EX) == ["닥스 검정 지갑 카드 2장"]      # 좁게 + 특징, 검색어 하나(임베딩 1번)


def test_full_query_category_fallback_and_empty():
    assert full_query({"item": "", "category": "전자기기", "color": "흰색"}) == "흰색 전자기기"
    assert build_queries({}) == []


def test_fanout_mode_keeps_old_behaviour():
    assert build_queries(EX, mode="fanout") == ["닥스 검정 지갑", "검정 지갑", "지갑"]  # 좁게→중간→넓게
    assert fanout_queries({"item": "지갑", "category": "지갑"}) == ["지갑"]           # 중복 제거
    assert fanout_queries({"item": "", "category": "전자기기"}) == ["전자기기"]
    assert fanout_queries({}) == []


def _capture_search(monkeypatch):
    seen = []
    monkeypatch.setattr(graph.search_mod, "search",
                        lambda q, **k: seen.append((q, k["size"])) or [
                            {"atc_id": f"{q}-{i}", "fd_sn": "1", "score": 1 - i / 100} for i in range(k["size"])])
    return seen


def test_stored_old_queries_are_rebuilt_from_extracted(monkeypatch):
    # 예전 방식으로 저장된 신고(검색어 3개)도 밤 재매칭부터 새 방식 — 한 번만 찾고, 판정 후보는 8개
    seen = _capture_search(monkeypatch)
    out = graph.n_search({"extracted": EX, "queries": ["닥스 검정 지갑", "검정 지갑", "지갑"], "region_set": []})
    assert seen == [("닥스 검정 지갑 카드 2장", 8)]
    assert len(out["candidates"]) == 8


def test_empty_extraction_falls_back_to_stored_query_then_text(monkeypatch):
    seen = _capture_search(monkeypatch)
    graph.n_search({"extracted": {}, "queries": ["검정 지갑", "지갑"], "region_set": []})
    graph.n_search({"extracted": {}, "queries": [], "region_set": [], "text": "JBL 무선 이어폰"})
    assert [q for q, _ in seen] == ["검정 지갑", "JBL 무선 이어폰"]


def test_fanout_mode_merges_three_queries(monkeypatch):
    monkeypatch.setattr(graph.settings, "search_query_mode", "fanout")
    seen = _capture_search(monkeypatch)
    out = graph.n_search({"extracted": EX, "queries": ["닥스 검정 지갑", "검정 지갑", "지갑"], "region_set": []})
    assert [q for q, _ in seen] == ["닥스 검정 지갑", "검정 지갑", "지갑"] and {s for _, s in seen} == {10}
    assert len(out["candidates"]) == 8                                        # 합친 뒤 판정 후보 수는 설정값
