"""색 모름 가점 — 습득물 색 '기타'(경찰이 색을 적지 않음: 우체통 습득 96%·공항 세관)는 '다른 색'이 아니라 '모름'.
임베딩은 '색상:기타'를 틀린 색처럼 봐서('검정 지갑': 검정 0.939·기타 0.893·갈색 0.890), 색을 말한 신고에서만 가점.
외부 서비스 없음: 임베딩·OpenSearch는 목킹.
"""

import pytest

from apps.api import search


def test_bonus_only_when_the_report_names_a_color_and_the_item_color_is_unknown():
    w = 0.025
    assert search.color_unknown_bonus("기타", "검정", w) == w
    assert search.color_unknown_bonus("기타", "black", w) == w
    assert search.color_unknown_bonus("블랙(검정)", "검정", w) == 0.0      # 색이 적힌 습득물은 임베딩이 이미 봄
    assert search.color_unknown_bonus("브라운(갈)", "검정", w) == 0.0
    for qc in ("", "  ", "모름", "모르겠어요", "알 수 없음", "기타", "unknown", "Not sure", "none", "various"):
        assert search.color_unknown_bonus("기타", qc, w) == 0.0, qc       # 색을 말하지 않은 신고는 '기타'도 같은 점수
    assert search.color_unknown_bonus("기타", "검정", 0.0) == 0.0         # 끄기


def _hit(i, score, color, dep="서울마포경찰서"):
    return {"_id": f"{i}_1", "_score": score, "_source": {
        "atc_id": i, "fd_sn": "1", "name": "지갑", "description": "", "category": "지갑 > 남성용 지갑", "color": color,
        "dep_place": dep, "found_at": "2026-11-10"}}


def _mock(monkeypatch, hits):
    monkeypatch.setattr(search.gemini, "embed_query", lambda q: [0.1] * 4)
    monkeypatch.setattr(search.resolver, "resolve", lambda dep: "서울특별시 마포구")
    monkeypatch.setattr(search, "_os_search", lambda body: hits)


def test_unknown_color_sits_between_right_and_wrong_color(monkeypatch):
    _mock(monkeypatch, [_hit("BLACK", 0.939, "블랙(검정)"), _hit("BROWN", 0.890, "브라운(갈)"), _hit("POSTBOX", 0.893, "기타")])
    out = search.search("검정 지갑", query_color="검정", w_color_unknown=0.025)
    assert [x["atc_id"] for x in out] == ["BLACK", "POSTBOX", "BROWN"]
    assert out[1]["score"] == pytest.approx(0.893 + 0.025)
    # 색을 말하지 않으면(추출 색 없음) 예전과 같음
    out = search.search("지갑", query_color="", w_color_unknown=0.025)
    assert [x["atc_id"] for x in out] == ["BLACK", "POSTBOX", "BROWN"] and out[1]["score"] == pytest.approx(0.893)


def test_graph_passes_the_extracted_color_to_search(monkeypatch):
    from apps.agent import graph

    seen = []
    monkeypatch.setattr(graph.search_mod, "search", lambda q, **k: seen.append(k["query_color"]) or [])
    graph.n_search({"text": "검정 지갑", "extracted": {"item": "지갑", "color": "검정"}, "region_set": []})
    graph.n_search({"text": "지갑", "extracted": {"item": "지갑"}, "region_set": []})
    assert seen == ["검정", ""]
