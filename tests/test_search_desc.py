"""특이사항 레인 — 내용물이 특이사항에만 적힌 습득물(경찰서 우체통 지갑: 이름 '지갑', 특이사항 '…외국인등록증')을
의미 검색 후보에 넣고, 특이사항이 신고 내용물과 맞으면 가점·신분증 종류가 어긋나면 감점.

실제 사례(11/17, Reddit 교환학생 ARC 지갑): F2026110500000551이 지역을 맞춰도 후보 100개 밖 → 레인 뒤 2위.
외부 서비스 없음: 임베딩·OpenSearch는 목킹.
"""

import pytest

from apps.api import search

BOILER = "내용\n\n 서울마포경찰서에서는 [2026.11.13]  [{name}(블랙(검정)색)]을 습득/보관 하였습니다. 특이사항 : {note}"


@pytest.fixture(autouse=True)
def _regions(monkeypatch):
    monkeypatch.setitem(search._REGION_VALUES, "values", [])
    monkeypatch.setitem(search._REGION_VALUES, "at", 0.0)


def test_content_groups_reads_korean_english_and_glued_words():
    assert search.content_groups("지갑 현금, 외국인등록증(ARC), 학생증") == ["외국인등록증", "학생증", "현금"]
    assert search.content_groups("black wallet with my ARC and Student ID") == ["외국인등록증", "학생증"]
    assert search.content_groups("기타 : 한양대학생증") == ["학생증"]                 # 붙여 써도
    assert search.content_groups("在留カードと学生証") == ["외국인등록증", "학생증"]


def test_content_groups_ignores_lookalikes():
    assert search.content_groups("Arc'teryx 검정 백팩") == []         # 브랜드 속 'Arc'는 외국인등록증이 아님
    assert search.content_groups("arc 모양 열쇠고리") == []             # 약자는 대문자만
    assert search.content_groups("국세청현금영수증카드") == []          # 현금영수증은 현금이 아님
    assert search.content_groups("카드지갑(현금 없음,) 신분증, 현금없음") == []   # 없다는 뜻
    assert search.content_groups("지갑에 현금은 없었고 여권이 있어요") == ["여권"]
    assert search.content_groups("검정 지갑 신분증 카드") == []         # 상투 문장에도 있는 '신분증'·흔한 '카드'는 안 셈


def _src(name="지갑", desc="", category="지갑 > 여성용 지갑"):
    return {"name": name, "category": category, "description": desc}


def test_bonus_only_for_contents_written_only_in_the_description():
    w = 0.05
    d, hit, clash = search.content_score(_src(desc="신용카드 : 1 건, 일반카드 : 1 건, 기타 : 외국인등록증"),
                                         ["외국인등록증", "학생증"], w)
    assert hit == ["외국인등록증"] and clash == [] and d == pytest.approx(0.05)
    # 이름에 있으면 임베딩이 이미 봄 → 가점 없음
    d, hit, _ = search.content_score(_src(name="지갑1, 외국인등록증", desc="외국인등록증"), ["외국인등록증"], w)
    assert d == 0.0 and hit == []
    # 경찰 상투 문장 속 이름은 특이사항이 아님(특이사항: 없음)
    boiler = BOILER.format(name="지갑, 외국인등록증", note="없음")
    assert search.content_score(_src(name="검정 지갑", desc=boiler), ["외국인등록증"], w)[0] == 0.0


def test_rarer_contents_weigh_more_and_total_is_capped():
    w = 0.05
    arc = search.content_score(_src(desc="외국인등록증"), ["외국인등록증", "학생증", "현금"], w)[0]
    sid = search.content_score(_src(desc="한양대 학생증"), ["외국인등록증", "학생증", "현금"], w)[0]
    cash = search.content_score(_src(desc="현금 : 5000 원"), ["외국인등록증", "학생증", "현금"], w)[0]
    both = search.content_score(_src(desc="외국인등록증/ 동국대학생증 / 현금"), ["외국인등록증", "학생증", "현금"], w)[0]
    assert arc > sid > cash > 0
    assert both == pytest.approx(w)                     # 여러 개 맞아도 w_desc까지(과가점 금지)
    assert search.content_score(_src(desc="외국인등록증"), ["외국인등록증"], 0.0) == (0.0, [], [])


def test_id_type_clash_is_penalised_both_ways():
    w = 0.05
    d, hit, clash = search.content_score(_src(name="지갑 주민등록증 현금"), ["외국인등록증", "현금"], w)
    assert clash == ["주민등록증"] and d == pytest.approx(-0.05)
    d, _, clash = search.content_score(_src(desc="기타 : 외국인등록증"), ["주민등록증"], w)
    assert clash == ["외국인등록증"] and d == pytest.approx(-0.05)
    # 둘 다 있으면(가족 지갑 등) 어긋남 아님, 학생증 가점은 그대로
    d, hit, clash = search.content_score(_src(desc="주민등록증, 외국인등록증, 학생증"), ["외국인등록증", "학생증"], w)
    assert clash == [] and hit == ["외국인등록증", "학생증"]


def test_lane_query_filters_region_date_and_excludes_name_matches():
    body = search.desc_lane_query([0.1] * 4, ["외국인등록증"], ["서울특별시 성동구"],
                                  {"range": {"found_at": {"gte": "2026-10-30"}}}, k=30)
    knn = body["query"]["knn"]["embedding"]
    assert knn["k"] == 30 and body["size"] == 30
    f = knn["filter"]["bool"]
    assert {"terms": {"region": ["서울특별시 성동구"]}} in f["filter"]
    assert {"range": {"found_at": {"gte": "2026-10-30"}}} in f["filter"]
    g = f["should"][0]["bool"]
    assert {"match_phrase": {"description": "외국인등록증"}} in g["should"]
    assert {"match_phrase": {"name": "외국인등록증"}} in g["must_not"]
    assert {"match_phrase": {"category": "외국인등록증"}} in g["must_not"]
    assert not any("在留" in str(x) for x in g["should"])       # 색인이 한국어라 한자 별칭은 거르기에 안 씀


def _hit(i, score, name="지갑", desc="", dep="서울성동경찰서"):
    return {"_id": f"{i}_1", "_score": score, "_source": {
        "atc_id": i, "fd_sn": "1", "name": name, "description": desc, "category": "지갑 > 여성용 지갑",
        "dep_place": dep, "found_at": "2026-11-02"}}


def _mock(monkeypatch, knn_hits, lane_hits, sent):
    monkeypatch.setattr(search.gemini, "embed_query", lambda q: [0.1] * 4)
    monkeypatch.setattr(search.resolver, "resolve", lambda dep: "서울특별시 성동구" if "성동" in dep else "서울특별시 마포구")
    monkeypatch.setattr(search, "_os_raw", lambda body: {"aggregations": {"r": {"buckets": [
        {"key": "서울특별시 성동구"}, {"key": "서울특별시 마포구"}, {"key": "부산광역시 중구"}]}}})

    def os_search(body):
        sent.append(body)
        return knn_hits

    def os_msearch(bodies):                                        # 레인: 묶음마다 한 질의 — 모든 질의에 같은 후보
        sent.extend(bodies)                                        # (여러 묶음에 걸린 습득물도 한 번만 들어가야 함)
        return [list(lane_hits) for _ in bodies]
    monkeypatch.setattr(search, "_os_search", os_search)
    monkeypatch.setattr(search, "_os_msearch", os_msearch)


def test_search_pulls_description_only_item_into_candidates(monkeypatch):
    """우체통 지갑(이름 '지갑', 특이사항 외국인등록증)이 의미 검색 후보에 없어도 레인으로 들어와 위로."""
    sent: list = []
    knn = [_hit("NAMED", 0.897, name="지갑1, 외국인등록증, 카드 4", dep="마포경찰서"),
           _hit("KOREAN", 0.880, name="지갑 주민등록증 현금")]
    lane = [_hit("POSTBOX", 0.848, desc="신용카드 : 1 건, 일반카드 : 1 건, 기타 : 외국인등록증"),
            _hit("NAMED", 0.897, name="지갑1, 외국인등록증, 카드 4", dep="마포경찰서")]   # 이미 있는 건 한 번만
    _mock(monkeypatch, knn, lane, sent)
    out = search.search("지갑 현금, 외국인등록증(ARC), 학생증", region_set=["마포", "성동"], lost_date="2026-10-31")
    assert [x["atc_id"] for x in out] == ["POSTBOX", "NAMED", "KOREAN"]   # 세 묶음 질의에 다 나와도 한 번씩
    assert out[0]["score"] == pytest.approx(0.848 + 0.05 + 0.05) and out[0]["desc_match"] == ["외국인등록증"]
    assert out[2]["id_clash"] == ["주민등록증"]                    # 외국인 신고에 주민등록증 지갑은 감점
    # 본 검색 1 + 레인: 묶음(외국인등록증·학생증·현금)을 합친 1 + 드문 외국인등록증만 따로 1
    # (합쳐서만 뽑으면 흔한 현금·학생증 습득물이 외국인등록증 습득물을 밀어냄)
    lanes = [b["query"]["knn"]["embedding"]["filter"]["bool"] for b in sent if "should" in str(b["query"])]
    assert len(sent) == 3 and len(lanes) == 2
    assert [len(f["should"]) for f in lanes] == [3, 1]
    assert {"match_phrase": {"description": "외국인등록증"}} in lanes[1]["should"][0]["bool"]["should"]
    for f in lanes:
        assert {"terms": {"region": ["서울특별시 성동구", "서울특별시 마포구"]}} in f["filter"]
        assert {"range": {"found_at": {"gte": "2026-10-30"}}} in f["filter"]


def test_lane_adds_a_separate_query_only_for_rare_ids_among_other_words(monkeypatch):
    """흔한 낱말끼리(현금·주민등록증)는 합친 질의 하나 — 묶음마다 따로 뽑으면 비슷한 지갑만 늘어 좋은 후보가 밀렸음."""
    def lanes_for(q):
        sent: list = []
        _mock(monkeypatch, [_hit("A", 0.90)], [], sent)
        search.search(q, region_set=["성동"], lost_date="2026-10-31")
        return [len(b["query"]["knn"]["embedding"]["filter"]["bool"]["should"]) for b in sent if "should" in str(b["query"])]

    assert lanes_for("지갑 현금 주민등록증") == [2]              # 흔한 것끼리 → 합친 하나
    assert lanes_for("지갑 외국인등록증") == [1]                 # 묶음 하나 → 하나
    assert lanes_for("passport and cash in my bag") == [2, 1]   # 여권(드묾)은 따로 한 번 더


def test_no_content_words_means_same_as_before(monkeypatch):
    sent: list = []
    knn = [_hit("A", 0.90), _hit("B", 0.88)]
    _mock(monkeypatch, knn, [_hit("LANE", 0.99)], sent)
    out = search.search("검정 지갑", region_set=["성동"], lost_date="2026-10-31")
    assert len(sent) == 1 and [x["atc_id"] for x in out] == ["A", "B"]


def test_lane_needs_a_region_and_can_be_turned_off(monkeypatch):
    sent: list = []
    _mock(monkeypatch, [_hit("A", 0.90)], [_hit("LANE", 0.99, desc="외국인등록증")], sent)
    search.search("지갑 외국인등록증", region_set=[], lost_date="2026-10-31")          # 지역 모름 → 전국은 너무 넓음
    assert len(sent) == 1
    sent.clear()
    out = search.search("지갑 외국인등록증", region_set=["성동"], lost_date="2026-10-31", w_desc=0.0)
    assert len(sent) == 1 and [x["atc_id"] for x in out] == ["A"] and out[0]["score"] == pytest.approx(0.95)


def test_lane_failure_keeps_semantic_results(monkeypatch):
    sent: list = []
    _mock(monkeypatch, [_hit("A", 0.90)], [], sent)

    def boom(body):
        raise OSError("opensearch down")
    monkeypatch.setattr(search, "_os_raw", boom)                 # 지역 값 목록 읽기 실패
    out = search.search("지갑 외국인등록증", region_set=["성동"], lost_date="2026-10-31")
    assert [x["atc_id"] for x in out] == ["A"]


def test_msearch_sends_one_ndjson_request_and_keeps_query_order(monkeypatch):
    """레인 묶음별 질의는 한 요청(_msearch) — 결과 순서는 질의 순서, 실패한 질의는 빈 목록."""
    import io
    import json

    seen = {}

    def fake_urlopen(req, timeout=30):
        seen["url"], seen["type"], seen["data"] = req.full_url, req.get_header("Content-type"), req.data.decode()
        return io.BytesIO(json.dumps({"responses": [
            {"hits": {"hits": [{"_id": "A_1"}]}}, {"error": {"type": "x"}}, {"hits": {"hits": []}}]}).encode())
    monkeypatch.setattr(search.urllib.request, "urlopen", fake_urlopen)
    out = search._os_msearch([{"size": 1}, {"size": 2}, {"size": 3}])
    assert out == [[{"_id": "A_1"}], [], []]
    assert seen["url"].endswith("/_msearch") and seen["type"] == "application/x-ndjson"
    lines = seen["data"].splitlines()
    assert lines == ["{}", '{"size": 1}', "{}", '{"size": 2}', "{}", '{"size": 3}'] and seen["data"].endswith("\n")


# ── 외국 운전면허증 (2026-11-21) ──
# 실제 사례(Reddit 홍대 지갑, 주인이 마포경찰서 보관 확인): 정답 특이사항 '…기타 : 외국 운전면허증(미국 캘리포니아)'.
# '운전면허증' 묶음만으로는 같은 지역의 국내 면허 지갑들이 같은 가점을 받아 정답이 29~31위 → 좁은 묶음으로 1위.

def test_foreign_license_is_a_narrow_group_in_any_language():
    for t in ("wallet US driver's license, credit cards", "It has his US driver’s license",
              "지갑 미국 운전면허증, 신용카드", "기타 : 외국 운전면허증(미국 캘리포니아)", "국제운전면허증",
              "운전면허증(미국)", "International Driving Permit", "Thai driving licence", "国際免許"):
        assert search.query_groups(t) == ["외국운전면허증"], t
    # 그냥 면허증은 넓은 묶음 그대로, 비슷한 꼴은 외국 면허가 아님
    for t in ("my driver's license", "운전면허증 : 1 건", "bus driver's license", "us driver license",
              "인도에서 운전면허증"):
        assert search.query_groups(t) == ["운전면허증"], t
    assert search.content_groups("외국 운전면허증 없음") == []              # 없다는 뜻


def test_foreign_license_report_prefers_foreign_license_items():
    w = 0.05
    gs = search.query_groups("wallet US driver's license, credit cards")
    us = search.content_score(_src(desc="신용카드 : 8 건, 일반카드 : 2 건, 기타 : 외국 운전면허증(미국 캘리포니아)"), gs, w)
    kr = search.content_score(_src(desc="운전면허증 : 1 건, 신용카드 : 3 건"), gs, w)
    assert us[0] == pytest.approx(0.05) and us[1] == ["외국운전면허증"]
    assert kr == (0.0, [], [])                                      # 국내 면허 지갑은 가점 없음(감점도 없음)
    # 거꾸로 그냥 'driver's license' 신고엔 외국 면허 습득물도 운전면허증 가점(외국인 신고면 대개 외국 면허)
    plain = search.content_score(_src(desc="기타 : 외국 운전면허증(미국 캘리포니아)"),
                                 search.query_groups("wallet with my driver's license"), w)
    assert plain[0] == pytest.approx(0.05 * 0.65) and plain[1] == ["운전면허증"]


def test_foreign_license_lane_ranks_the_postbox_wallet_first(monkeypatch):
    """홍대 지갑 꼴: 의미 검색엔 국내 면허 지갑만, 레인이 외국 면허 우체통 지갑을 데려와 1위로."""
    sent: list = []
    knn = [_hit("KR1", 0.902, desc="운전면허증 : 1 건, 신용카드 : 3 건", dep="마포경찰서"),
           _hit("KR2", 0.900, desc="운전면허증 : 1 건, 신용카드 : 5 건", dep="마포경찰서")]
    lane = [_hit("US", 0.875, desc="신용카드 : 8 건, 일반카드 : 2 건, 기타 : 외국 운전면허증(미국 캘리포니아)",
                 dep="마포경찰서")]
    _mock(monkeypatch, knn, lane, sent)
    out = search.search("wallet US driver's license, credit cards", region_set=["마포"], lost_date="2026-11-11")
    assert [x["atc_id"] for x in out] == ["US", "KR1", "KR2"]
    assert out[0]["desc_match"] == ["외국운전면허증"]
    lanes = [b["query"]["knn"]["embedding"]["filter"]["bool"] for b in sent if "should" in str(b["query"])]
    assert len(lanes) == 1                                          # 묶음 하나 → 레인 질의 하나
    phrases = [x["match_phrase"]["description"] for x in lanes[0]["should"][0]["bool"]["should"]]
    assert "미국 운전면허" in phrases and "국제면허" in phrases and "운전면허증" not in phrases


def test_foreign_writer_reads_a_plain_license_as_foreign():
    """원글('…get his drivers license back', 미국 관광객)처럼 외국어 신고의 그냥 면허증은 외국 면허로."""
    assert search.query_groups("wallet drivers license, credit cards", foreign_writer=True) == ["외국운전면허증"]
    assert search.query_groups("wallet drivers license, credit cards") == ["운전면허증"]
    assert search.query_groups("passport and cash", foreign_writer=True) == ["여권", "현금"]   # 다른 묶음은 그대로


def test_graph_tells_search_whether_the_report_is_in_a_foreign_language(monkeypatch):
    from apps.agent import graph

    assert graph.foreign_writer({"lang": "en", "text": "지갑"})
    assert graph.foreign_writer({"lang": "ko", "text": "Lost my wallet in Hongdae"})   # MCP는 언어를 안 주면 ko
    assert not graph.foreign_writer({"lang": "ko", "text": "홍대에서 지갑을 잃어버렸어요"})
    assert not graph.foreign_writer({})
    seen = []
    monkeypatch.setattr(graph.search_mod, "search", lambda q, **k: seen.append(k["foreign_writer"]) or [])
    ex = {"item": "wallet", "features": "drivers license"}
    graph.n_search({"text": "lost wallet with drivers license", "lang": "en", "extracted": ex, "region_set": []})
    graph.n_search({"text": "지갑에 운전면허증", "lang": "ko", "extracted": ex, "region_set": []})
    assert seen == [True, False]
