"""버스 노선표 경유 지역 — 번호는 표에서, 나머지는 Gemini(fallback). 표·해석기는 정적 데이터라 외부 호출 없음."""

from __future__ import annotations

import pytest

from apps.agent import route
from scripts.build_bus_routes import (
    _home_city,
    molit_key,
    molit_routes,
    region_names,
    route_key,
    tago_routes,
)


def _r(name, key, city, regions, corp="", kind="간선"):
    return {"name": name, "key": key, "city": city, "regions": regions, "corp": corp, "kind": kind}


# 실제 노선표(eval/data/bus_routes.json)에서 옮긴 일부
ROUTES = {
    "seoul:146": _r("146", "146", "서울", ["서울 노원구", "서울 도봉구", "서울 중랑구", "서울 광진구", "서울 성동구",
                                          "서울 강남구", "서울 서초구"], "삼화상운"),
    "seoul:102": _r("102", "102", "서울", ["서울 노원구", "서울 도봉구", "서울 강북구", "서울 성북구", "서울 종로구"]),
    "seoul:102sn": _r("102성남", "102", "성남", ["경기 용인시", "경기 성남시", "서울 송파구", "서울 강남구", "서울 광진구"],
                      kind="경기"),
    "seoul:320": _r("320", "320", "서울", ["서울 송파구", "서울 광진구", "서울 중랑구"]),
    "seoul:320as": _r("320안산", "320", "안산", ["경기 안산시", "경기 시흥시", "경기 광명시", "서울 구로구",
                                               "서울 영등포구"], kind="경기"),
    "seoul:6001": _r("6001", "6001", "서울", ["인천 영종구", "인천 중구", "서울 강서구", "서울 중구"], kind="공항"),
    "seoul:N37": _r("N37", "N37", "서울", ["서울 송파구", "서울 강남구", "서울 은평구"], kind="심야"),
    "seoul:gn01": _r("강남01", "강남01", "서울", ["서울 강남구"], kind="마을"),
    "seoul:9401": _r("9401", "9401", "서울", ["경기 성남시", "서울 서초구", "서울 중구"], kind="광역"),
}
R146 = ROUTES["seoul:146"]["regions"]


class FakeLLM:
    def __init__(self, out=("GEMINI",)):
        self.calls, self.out = [], list(out)

    def expand(self, place):
        self.calls.append(place)
        return list(self.out)


@pytest.fixture
def hybrid():
    llm = FakeLLM()
    return route.HybridRouteResolver(route.BusTable(ROUTES), llm), llm


@pytest.mark.parametrize("ctx", ["146번 버스", "서울 146번 버스", "146번 버스 안에서", "버스 146", "146 bus"])
def test_bus_number_uses_real_route_without_gemini(hybrid, ctx):
    r, llm = hybrid
    assert r.expand(ctx) == R146
    assert llm.calls == []


@pytest.mark.parametrize("ctx", ["2호선", "부산역 기차", "홍대입구역", "3번 출구 앞 버스정류장", "9999번 버스", ""])
def test_not_a_known_bus_falls_back_to_gemini(hybrid, ctx):
    r, llm = hybrid
    out = r.expand(ctx)
    assert llm.calls == [ctx] and out == ["GEMINI"]


def test_other_city_bus_number_goes_to_gemini(hybrid):
    """표엔 서울·성남·광명 102번뿐 — '부산 102번'은 표의 어느 노선도 부산을 안 지나므로 Gemini."""
    r, llm = hybrid
    assert r.expand("부산 102번 버스") == ["GEMINI"] and llm.calls == ["부산 102번 버스"]


def test_same_number_prefers_seoul_without_place(hybrid):
    r, _ = hybrid
    assert r.expand("102번 버스") == ROUTES["seoul:102"]["regions"]
    assert r.expand("320번 버스") == ROUTES["seoul:320"]["regions"]


def test_place_in_text_picks_that_city_route(hybrid):
    """'안산' → 경기 → 경기를 지나는 320안산만(서울 320은 경기를 안 지남). 안산은 이미 노선 구라 더하지 않음."""
    r, llm = hybrid
    assert r.expand("320번 버스 안산") == ROUTES["seoul:320as"]["regions"]
    assert llm.calls == []


def test_airport_bus_with_incheon_airport_keeps_route(hybrid):
    """'인천공항'은 시도(인천광역시)까지만 풀림 → 6001(인천을 지남) 유지, 인천 전체는 더하지 않음."""
    r, llm = hybrid
    assert r.expand("6001번 공항버스 인천공항") == ROUTES["seoul:6001"]["regions"]
    assert llm.calls == []


def test_extra_place_off_route_adds_only_its_district(hybrid):
    """노선 밖 장소는 그 구만 더함(Gemini의 인접 구 나열 없이). 명동 → 서울 중구."""
    r, llm = hybrid
    assert r.expand("146번 버스 타고 명동") == R146 + ["서울 중구"]
    assert llm.calls == []


def test_unknown_extra_place_asks_gemini_for_that_part_only(hybrid):
    r, llm = hybrid
    out = r.expand("146번 버스, 사가정역")
    assert llm.calls == ["번 버스, 사가정역"]           # 번호는 빼고 장소 부분만
    assert out == R146 + ["GEMINI"]


def test_number_glued_to_station_name(hybrid):
    r, llm = hybrid
    out = r.expand("사가정역146번 버스")
    assert out[: len(R146)] == R146 and llm.calls and "사가정역" in llm.calls[0]


@pytest.mark.parametrize("ctx,key", [("N37번 심야버스", "seoul:N37"), ("강남01 마을버스", "seoul:gn01"),
                                     ("9401번 광역버스 분당", "seoul:9401")])
def test_night_village_and_red_buses(hybrid, ctx, key):
    r, llm = hybrid
    assert r.expand(ctx) == ROUTES[key]["regions"] and llm.calls == []


def test_two_buses_union(hybrid):
    r, _ = hybrid
    out = r.expand("146번 버스 타고 가다 320번 버스로 갈아탐")
    assert set(out) == set(R146) | set(ROUTES["seoul:320"]["regions"])


def test_table_regions_are_not_truncated_but_extras_are(hybrid):
    long = [f"서울 구{i}" for i in range(18)]
    llm = FakeLLM(out=[f"추가{i}" for i in range(10)])
    r = route.HybridRouteResolver(route.BusTable({"x": _r("777", "777", "서울", long)}), llm)
    out = r.expand("777번 버스, 무지개빵집")
    assert out[:18] == long and len(out) == route.MAX_REGIONS


def test_missing_table_behaves_like_before(tmp_path):
    llm = FakeLLM()
    r = route.HybridRouteResolver(route.BusTable.load(tmp_path / "없음.json"), llm)
    assert r.expand("146번 버스") == ["GEMINI"]


def test_matches_index_regions():
    """노선표 꼴('서울 노원구'·'경기 고양시')이 색인 꼴('서울특별시 노원구'·'경기도 고양시 일산서구')과 맞는다."""
    from apps.api.region import region_matches
    assert region_matches("서울특별시 노원구", ["서울 노원구"])
    assert region_matches("경기도 고양시 일산서구", ["경기 고양시"]) and region_matches("경기도 고양시", ["경기 고양시"])
    assert not region_matches("부산광역시 중구", ["서울 중구"])


@pytest.mark.parametrize("name,rtype,out", [
    ("146", "3", ("146", "서울")), ("N37", "15", ("N37", "서울")), ("서울01퇴근", "6", ("서울01", "서울")),
    ("8775출근", "4", ("8775", "서울")), ("320안산", "8", ("320", "안산")), ("86인천", "7", ("86", "인천")),
    ("5001(예약)용인", "8", ("5001", "용인")), ("5001-1(급행하행)용인", "8", ("5001-1", "용인")),
    ("G7426파주", "8", ("G7426", "파주")), ("1234예약수원", "8", ("1234", "수원")),
])
def test_route_key(name, rtype, out):
    assert route_key(name, rtype) == out


@pytest.mark.parametrize("raw,out", [
    ("서울특별시 노원구", ["서울 노원구"]), ("경기도 고양시 일산서구", ["경기 고양시"]),
    ("인천광역시 영종구", ["인천 영종구", "인천 중구"]), ("세종특별자치시", ["세종"]),
    ("인천광역시 중구", ["인천 중구", "인천 영종구", "인천 제물포구"]), ("강원특별자치도 춘천시", ["강원 춘천시"]),
    ("전남광주통합특별시 광산구", ["광주 광산구"]), ("", []),
])
def test_region_names(raw, out):
    assert region_names(raw) == out


# ---------- 전국(국토부) 출처 ----------
NATION = {
    **ROUTES,
    "molit:b102": _r("102", "102", "부산", ["부산 해운대구", "부산 수영구", "부산 남구"], kind=""),
    "molit:b7": _r("7", "7", "부산", ["부산 중구", "부산 서구", "부산 영도구"], kind=""),
    "molit:sw7": _r("7", "7", "수원", ["경기 수원시", "경기 화성시"], kind=""),
    "molit:yi7": _r("7", "7", "용인", ["경기 용인시", "경기 성남시"], kind=""),
    "molit:dg7": _r("7", "7", "대구", ["대구 중구", "대구 동구"], kind=""),
    "molit:jj7": _r("7", "7", "제주", ["제주 제주시"], kind=""),
}


@pytest.fixture
def nation():
    llm = FakeLLM()
    return route.HybridRouteResolver(route.BusTable(NATION), llm), llm


@pytest.mark.parametrize("ctx,key", [("부산 102번 버스", "molit:b102"), ("해운대에서 102번 버스", "molit:b102"),
                                     ("부산 7번 버스", "molit:b7"), ("수원 7번 버스", "molit:sw7"),
                                     ("제주 7번 버스", "molit:jj7")])
def test_other_city_number_uses_that_city_route(nation, ctx, key):
    """같은 번호가 여러 도시에 있으면 문장 속 지명으로 — 도(경기)면 시군까지 맞는 노선('수원 7번' → 수원 7번만)."""
    r, llm = nation
    assert r.expand(ctx) == NATION[key]["regions"] and llm.calls == []


def test_nationwide_table_keeps_seoul_first_without_place(nation):
    r, llm = nation
    assert r.expand("102번 버스") == ROUTES["seoul:102"]["regions"] and llm.calls == []


def test_number_in_many_cities_without_place_goes_to_gemini(nation):
    """서울엔 없고 부산·수원·용인·대구·제주에 다 있는 7번 — 고를 근거가 없음."""
    r, llm = nation
    assert r.expand("7번 버스") == ["GEMINI"] and llm.calls == ["7번 버스"]


def test_branch_numbers_found_by_main_number():
    """제주는 국토부 자료에 '201-11'·'201-12'(갈래)로 오지만 사람은 '201번' — 본 번호로도 찾음(11/18 실측: 못 찾던 것).
    서울 '15-1'처럼 같은 도시에 본 번호 노선이 따로 있으면 섞지 않음."""
    t = route.BusTable({
        **ROUTES,
        "molit:j1": _r("201-11", "201-11", "제주", ["제주 제주시", "제주 서귀포시"], kind=""),
        "molit:j2": _r("201-12", "201-12", "제주", ["제주 서귀포시", "제주 제주시"], kind=""),
        "seoul:15": _r("15", "15", "서울", ["서울 강북구", "서울 종로구"]),
        "seoul:15-1": _r("15-1", "15-1", "서울", ["서울 은평구"]),
    })
    assert t.lookup("제주 201번 버스") == (["제주 제주시", "제주 서귀포시"], "")
    assert t.lookup("15번 버스")[0] == ["서울 강북구", "서울 종로구"]
    assert t.lookup("15-1번 버스")[0] == ["서울 은평구"]


@pytest.mark.parametrize("no,key", [("1500(예약)", "1500"), ("15-1(구파발)", "15-1"), ("171번", "171"),
                                    ("급행1", "급행1"), ("750a", "750A"), ("좌석 02", "좌석02"),
                                    (None, ""), ("", "")])  # 번호 없는 시외·공항 노선
def test_molit_key(no, key):
    assert molit_key(no) == key


@pytest.mark.parametrize("label,city", [("경기도 고양시 덕양구", "고양"), ("부산광역시 중구", "부산"),
                                        ("세종특별자치시", "세종"), ("제주특별자치도 서귀포시", "서귀포"),
                                        ("전라남도 화순군", "화순"), ("전남광주통합특별시 서구", "광주"),
                                        ("전남광주통합특별시 나주시", "나주"), ("인천광역시 영종구", "인천")])
def test_home_city(label, city):
    assert _home_city(label) == city


def _seg(no, lo, hi, n, first="", last=""):
    return {"no": no, "nm": no, "min": lo, "max": hi, "n": n, "first": first, "last": last}


def test_molit_routes_join_districts_in_route_order():
    """시군구별로 따로 온 정류장 → 노선 하나로. 구는 노선 순서(정류장 순번)대로, 도시는 정류장이 가장 많은 곳."""
    sggs = {
        "29140": {"label": "광주광역시 서구", "routes": {"1|좌석02": _seg("좌석02", 20, 40, 15, "", "광천터미널")}},
        "46170": {"label": "전라남도 나주시", "routes": {"1|좌석02": _seg("좌석02", 1, 19, 19, "나주역", "")}},
        "41281": {"label": "경기도 고양시 덕양구", "routes": {"2|171": _seg("171", 40, 45, 3)}},
        "11110": None,
    }
    seoul = {"seoul:171": _r("171", "171", "서울", ["서울 강북구", "서울 종로구", "경기 고양시", "서울 마포구"])}
    out, stat = molit_routes(sggs, seoul)
    assert list(out) == ["molit:1:좌석02"]                       # 171은 서울 출처와 같은 노선이라 뺌
    r = out["molit:1:좌석02"]
    assert r["regions"] == ["전남 나주시", "광주 서구"] and r["city"] == "나주"
    assert (r["start"], r["end"], r["start_region"]) == ("나주역", "광천터미널", "전남 나주시")
    assert stat["서울·TAGO 출처와 같음(뺌)"] == 1


def test_molit_routes_keep_same_number_on_a_different_route():
    """같은 번호라도 구가 거의 안 겹치면 다른 노선(고양 720 ≠ 서울 720) — 넣음."""
    sggs = {"41281": {"label": "경기도 고양시 덕양구", "routes": {"9|720": _seg("720", 1, 30, 30)}}}
    seoul = {"seoul:720": _r("720", "720", "서울", ["서울 은평구", "서울 서대문구", "서울 동대문구"])}
    out, _ = molit_routes(sggs, seoul)
    assert out["molit:9:720"]["regions"] == ["경기 고양시"]


class _Resp:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _molit_answers(monkeypatch, *bodies):
    from scripts import build_bus_routes as b
    calls = []
    seq = list(bodies)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    monkeypatch.setattr(b.urllib.request, "urlopen",
                        lambda url, timeout=0: calls.append(url) or _Resp(seq.pop(0) if len(seq) > 1 else seq[0]))
    return b, calls


_GATE = '{"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "%s", "returnReasonCode": "%s"}}}'
_OK = ('{"Response": {"header": {"resultCode": "200"}, "body": {"items": {"item": {"rte_id": "1", "rte_no": "7", '
       '"sttn_seq": 3, "ctpv_nm": "부산광역시", "sgg_nm": "중구"}}, "totalCount": 1}}}')


def test_molit_no_data_is_empty_not_error(monkeypatch):
    b, _ = _molit_answers(monkeypatch, '{"Error": {"code": "50", "message": "NO_DATA_FOUND"}}')
    assert b.molit_page("26110", "20261022") == (0, [])


def test_molit_gateway_hiccup_retries_then_succeeds(monkeypatch):
    """국토부 제공 서버가 04 HTTP_ERROR를 자주 냄(11/17) — 잠시 뒤 다시 물으면 됨. 한 건짜리 item(딕셔너리)도 목록으로."""
    b, calls = _molit_answers(monkeypatch, _GATE % ("HTTP_ERROR", "04"), _OK)
    total, rows = b.molit_page("26110", "20261022")
    assert total == 1 and rows[0]["rte_no"] == "7" and len(calls) == 2


def test_molit_gateway_keeps_failing_skips_that_district(monkeypatch):
    b, calls = _molit_answers(monkeypatch, _GATE % ("HTTP_ERROR", "04"))
    with pytest.raises(b.ApiFlaky):
        b.molit_page("26110", "20261022")
    assert len(calls) == 5


def test_molit_quota_or_key_error_stops_everything(monkeypatch):
    """한도 초과(22)·키 오류는 다시 물어도 소용없음 — 건너뛰기가 아니라 멈춤."""
    b, calls = _molit_answers(monkeypatch, _GATE % ("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "22"))
    with pytest.raises(b.ApiStop) as e:
        b.molit_page("26110", "20261022")
    assert not isinstance(e.value, b.ApiFlaky) and len(calls) == 1


# ---------- 대구·광주 시내버스(TAGO) ----------
def test_tago_routes_regions_in_stop_order():
    """정류장 순서대로 좌표 → 구. 대구 급행1은 경산까지, 광주는 통합특별시 이름이어도 '광주 ○구'로."""
    coords = {"a": "대구광역시 동구", "b": "대구광역시 동구", "c": "경상북도 경산시", "d": "전남광주통합특별시 서구",
              "e": "전남광주통합특별시 화순군"}
    cache = {"DGB1": {"city": "22", "no": "급행1", "tp": "급행버스", "start": "동화사", "end": "매곡", "stops": ["a", "b", "c"]},
             "KJB2": {"city": "24", "no": "01", "tp": "간선버스", "start": "세하동", "end": "증심사", "stops": ["d", "e", "x"]},
             "KJB9": {"city": "24", "no": "", "tp": "", "start": "", "end": "", "stops": ["d"]}}
    out = tago_routes(cache, coords)
    assert out["tago:22:DGB1"]["regions"] == ["대구 동구", "경북 경산시"] and out["tago:22:DGB1"]["city"] == "대구"
    assert out["tago:24:KJB2"]["regions"] == ["광주 서구", "전남 화순군"] and out["tago:24:KJB2"]["start_region"] == "광주 서구"
    assert "tago:24:KJB9" not in out                                   # 번호 없는 노선은 번호로 못 찾음


def _tago_ok(items, total=None):
    import json as _j
    return _j.dumps({"response": {"header": {"resultCode": "00"}, "body": {
        "items": {"item": items} if items != "" else "",
        "totalCount": (len(items) if isinstance(items, list) else 1) if total is None else total}}})


def test_tago_single_item_and_empty_items(monkeypatch):
    b, _ = _molit_answers(monkeypatch, _tago_ok({"routeid": "DGB1", "routeno": 503}))
    assert b.tago("getRouteNoList", cityCode="22") == (1, [{"routeid": "DGB1", "routeno": 503}])
    b, _ = _molit_answers(monkeypatch, _tago_ok("", total=0))
    assert b.tago("getRouteNoList", cityCode="22") == (0, [])


def test_tago_gateway_hiccup_retries_but_quota_stops(monkeypatch):
    xml = "<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>%s</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
    b, calls = _molit_answers(monkeypatch, xml % "04", _tago_ok([{"routeid": "x"}]))
    assert b.tago("getRouteNoList", cityCode="24")[0] == 1 and len(calls) == 2
    b, calls = _molit_answers(monkeypatch, xml % "22")
    with pytest.raises(b.ApiStop):
        b.tago("getRouteNoList", cityCode="24")
    assert len(calls) == 1
