"""분실일 거름 — 분실일을 적으면 그 전날 이후 습득물만 찾음(검색 단계 KNN filter), 전날 습득물은 조금 낮춤.

운영 실측(11/15): 날짜 없이 비슷한 순 100개를 뽑으면 분실일 이후 습득물이 0~19개뿐 → 거름이 없으면 진짜 물건이 후보에 못 들어옴.
외부 서비스 없음: 임베딩·OpenSearch는 목킹.
"""

from datetime import date

from apps.agent import graph
from apps.api import search


def test_date_floor_is_the_day_before_and_ignores_bad_dates():
    today = date(2026, 11, 15)
    assert search.date_floor("2026-11-13", 1, today) == "2026-11-12"
    assert search.date_floor("2026-11-15", 1, today) == "2026-11-14"
    assert search.date_floor(None, 1, today) is None
    assert search.date_floor("어제", 1, today) is None              # 형식이 이상하면 거르지 않음
    assert search.date_floor("2026-12-01", 1, today) is None        # 미래 날짜 하나로 결과가 통째로 비지 않게


def test_date_penalty_only_for_items_found_before_the_lost_day():
    assert search.date_penalty("2026-11-12", "2026-11-13", 0.02) == 0.02   # 전날
    assert search.date_penalty("2026-11-13", "2026-11-13", 0.02) == 0.0    # 같은 날
    assert search.date_penalty("2026-11-14", "2026-11-13", 0.02) == 0.0    # 뒤
    assert search.date_penalty("2026-11-12", None, 0.02) == 0.0


def _hit(i, found_at, score=0.9):
    return {"_id": f"{i}_1", "_score": score,
            "_source": {"atc_id": i, "fd_sn": "1", "name": "지갑", "found_at": found_at, "dep_place": ""}}


def test_search_filters_knn_by_lost_date_and_puts_same_day_first(monkeypatch):
    sent = []
    monkeypatch.setattr(search.gemini, "embed_query", lambda q: [0.1] * 4)
    monkeypatch.setattr(search, "_os_search", lambda body: sent.append(body) or [
        _hit("DAYBEFORE", "2026-11-12"), _hit("SAMEDAY", "2026-11-13")])      # 점수 같음
    out = search.search("검정 지갑", lost_date="2026-11-13")
    knn = sent[0]["query"]["knn"]["embedding"]
    assert knn["filter"] == {"range": {"found_at": {"gte": "2026-11-12"}}} and knn["k"] == 100
    assert [x["atc_id"] for x in out] == ["SAMEDAY", "DAYBEFORE"]           # 전날 습득은 조금 아래로


def test_search_without_lost_date_does_not_filter(monkeypatch):
    sent = []
    monkeypatch.setattr(search.gemini, "embed_query", lambda q: [0.1] * 4)
    monkeypatch.setattr(search, "_os_search", lambda body: sent.append(body) or [])
    search.search("검정 지갑")
    assert "filter" not in sent[0]["query"]["knn"]["embedding"]


def test_keyword_fallback_also_filters_by_lost_date(monkeypatch):
    sent = []

    def down(q):
        raise OSError("gemini down")

    monkeypatch.setattr(search.gemini, "embed_query", down)
    monkeypatch.setattr(search, "_os_search", lambda body: sent.append(body) or [])
    search.search("검정 지갑", lost_date="2026-11-13")
    q = sent[0]["query"]["bool"]
    assert "multi_match" in q["must"][0] and q["filter"] == [{"range": {"found_at": {"gte": "2026-11-12"}}}]


def test_agent_search_uses_extracted_lost_date(monkeypatch):
    seen = []
    monkeypatch.setattr(graph.search_mod, "search", lambda q, **k: seen.append(k["lost_date"]) or [])
    graph.n_search({"extracted": {"lost_date": "2026-11-13"}, "queries": ["검정 지갑", "지갑"], "region_set": []})
    assert seen == ["2026-11-13", "2026-11-13"]                              # 재매칭(match_stored)도 같은 경로
