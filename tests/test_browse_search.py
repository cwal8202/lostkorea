"""목록(전체 습득물) 키워드 검색 — 낱말을 더할수록 좁아지게, 색 이름·동의어, 다 맞은 것 먼저, 시도 고르면 그 아래 전부.

운영 실측(11/15): 예전엔 낱말 중 하나만 있어도 나와(OR) '검정 지갑' 69,384건 > '지갑' 33,650건,
'갈색 가죽 지갑'은 경찰 데이터가 '브라운(갈)'로 적어 모두 포함으로 바꾸면 33건뿐, '수모'·'수영모'가 따로 놀았음.
외부 서비스 없음: OpenSearch는 목킹(보낸 질의를 검사).
"""

import pytest

from apps.api import search


@pytest.fixture
def sent(monkeypatch):
    bodies = []
    monkeypatch.setattr(search, "_os_raw", lambda body: bodies.append(body) or {
        "hits": {"hits": [], "total": {"value": 25_000}},
        "aggregations": {"regions": {"buckets": [
            {"key": "서울특별시 강남구", "doc_count": 30}, {"key": "서울특별시", "doc_count": 50},
            {"key": "부산광역시 해운대구", "doc_count": 40}, {"key": "서울특별시 중구", "doc_count": 25}, {"key": "", "doc_count": 9}]}}})
    return bodies


def _groups(q):
    return search.keyword_query(q)["bool"]


def test_one_or_two_words_must_all_match_three_or_more_may_miss_one():
    assert _groups("지갑")["minimum_should_match"] == 1
    assert _groups("검정 지갑")["minimum_should_match"] == 2                      # 예전 OR: 하나만 있어도
    assert _groups("갈색 가죽 지갑")["minimum_should_match"] == 2                 # 3낱말 → 하나는 빠져도
    g = _groups("검정지갑")["should"][0]["dis_max"]["queries"][0]["constant_score"]["filter"]["multi_match"]
    assert g["operator"] == "and" and g["type"] == "cross_fields"              # 붙여 쓴 말도 그 안의 말 모두


def _alts(q, expand=False):
    return [(m["constant_score"]["filter"]["multi_match"]["query"], m["constant_score"]["boost"])
            for m in search.keyword_query(q, expand)["bool"]["should"][0]["dis_max"]["queries"]]


def test_color_words_and_synonyms_are_grouped():
    assert [a for a, _ in _alts("갈색")] == ["갈색", "브라운"]                     # 경찰 데이터 표기 '브라운(갈)'
    assert [a for a, _ in _alts("수모")] == ["수모", "수영모"] and [a for a, _ in _alts("핸드폰")] == ["핸드폰", "휴대폰"]
    assert _alts("지갑") == [("지갑", 2)]


def test_same_word_spellings_both_ways_brands_to_generic_one_way_exact_first():
    assert [a for a, _ in _alts("보조밧데리")][1:] == ["보조배터리", "보조베터리"]   # 표기만 다른 말 — 양방향
    assert "보조밧데리" in [a for a, _ in _alts("보조배터리")]
    assert _alts("에어팟") == [("에어팟", 2)]                                     # 브랜드 → 일반 이름은 기본으로 안 섞음
    assert _alts("에어팟", True) == [("에어팟", 2), ("무선이어폰", 1)]            # '비슷한 이름도 보기' — 친 그대로가 먼저
    assert _alts("무선이어폰") == [("무선이어폰", 2)]                             # 거꾸로는 안 넓힘(에어팟만 나오면 안 되니까)
    assert [a for a, _ in _alts("IQOS")] == ["IQOS", "아이코스"]                  # 영문은 소문자로 찾아 봄
    assert [a for a, _ in _alts("하나카드")] == ["하나카드"]                       # 너무 넓은 '하나' 같은 건 안 넣음


def test_full_matches_first_then_date_order(sent):
    search.browse(q="갈색 가죽 지갑")
    assert sent[0]["sort"][0] == {"_score": {"order": "desc"}} and "found_at" in sent[0]["sort"][1]
    search.browse(sort="old")
    assert sent[1]["sort"] == [{"found_at": {"order": "asc", "missing": "_last"}}]   # 키워드 없으면 날짜순만


def test_choosing_a_province_includes_its_districts(sent):
    search.browse(region="서울특별시")
    f = sent[0]["query"]["bool"]["filter"][0]["bool"]["should"]
    assert {"term": {"region": "서울특별시"}} in f and {"prefix": {"region": "서울특별시 "}} in f


def test_region_dropdown_is_province_then_districts_and_pages_stop_at_the_window(sent):
    d = search.browse()
    assert [(r["name"], r["count"], r["level"]) for r in d["regions"]] == [
        ("서울특별시", 105, 0), ("서울특별시 강남구", 30, 1), ("서울특별시 중구", 25, 1),
        ("부산광역시", 40, 0), ("부산광역시 해운대구", 40, 1)]
    assert d["regions"][1]["label"] == "강남구"
    assert d["max_page"] == 10_000 // 24                                        # 25,000건이어도 열 수 있는 데까지만


def _fake_totals(monkeypatch, exact, wide):
    """일반 이름(무선이어폰·노트북)이 든 질의면 wide건, 아니면 exact건 — 넓히기 판단만 검사."""
    sent = []

    def fake(body):
        sent.append(body)
        n = wide if any(w in str(body["query"]) for w in ("무선이어폰", "노트북")) else exact
        return {"hits": {"hits": [], "total": {"value": n}}, "aggregations": {"regions": {"buckets": []}}}
    monkeypatch.setattr(search, "_os_raw", fake)
    return sent


def test_brand_to_generic_is_a_button_not_mixed_in(monkeypatch):
    _fake_totals(monkeypatch, exact=5020, wide=27069)
    d = search.browse(q="에어팟")
    assert d["total"] == 5020 and d["expand"]["on"] is False and d["expand"]["extra"] == 22049   # 기본은 그대로만 + 단추 건수
    assert d["expand"]["words"] == ["무선이어폰"] and d["expand"]["words_en"] == ["wireless earbuds"]
    d = search.browse(q="에어팟", expand=True)
    assert d["total"] == 27069 and d["expand"]["on"] and d["expand"]["exact"] == 5020 and not d["expand"]["auto"]
    assert search.browse(q="무선이어폰")["expand"] is None and search.browse(q="지갑", expand=True)["expand"] is None


def test_too_few_exact_results_are_widened_automatically_and_said_so(monkeypatch):
    sent = _fake_totals(monkeypatch, exact=7, wide=300)
    d = search.browse(q="맥북")
    assert d["total"] == 300 and d["expand"]["auto"] and d["expand"]["exact"] == 7      # 20건 미만 → 이어 붙이고 알림
    assert "노트북" in str(sent[-1]["query"])
