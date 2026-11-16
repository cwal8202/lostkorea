"""버스 노선표 경유 지역 — 번호는 표에서, 나머지는 Gemini(fallback). 표·해석기는 정적 데이터라 외부 호출 없음."""

from __future__ import annotations

import pytest

from apps.agent import route
from scripts.build_bus_routes import region_names, route_key


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
    ("전남광주통합특별시 광산구", ["광주 광산구"]), ("", []),
])
def test_region_names(raw, out):
    assert region_names(raw) == out
