"""지역 해석기 — dep(보관장소)→구/시, 그리고 넓은 구/시 집합과의 매칭(가점 판정 핵심)."""

from apps.api.region import RegionResolver, resolver


def test_match_substring_broad():
    # region_set의 짧은이름이 해석된 지역 문자열에 포함되면 매칭(넓게 잡기)
    assert RegionResolver.match("서울특별시 강남구", ["강남"]) is True
    assert RegionResolver.match("서울특별시 강남구", ["서초", "강남"]) is True
    assert RegionResolver.match("부산광역시 해운대구", ["강남"]) is False


def test_match_empty_never_true():
    assert RegionResolver.match("서울특별시 강남구", []) is False
    assert RegionResolver.match("", ["강남"]) is False
    assert RegionResolver.match("서울특별시 강남구", [""]) is False  # 빈 항목은 무시


def test_resolver_loaded_and_safe():
    # gazetteer가 로드되어야 하고, 미상 입력은 빈 문자열(크래시 없음)
    assert resolver.stations and resolver.tokens
    assert resolver.resolve("") == ""
    assert isinstance(resolver.resolve("존재하지않는장소_xyz_123"), str)


def test_resolve_token_hit():
    # 지역 토큰(구/시 짧은이름)이 dep 문자열에 들어있으면 그 토큰으로 해석 — 시도를 채워 '시도 시군구'로
    tok = resolver.tokens[0]
    assert resolver.resolve(f"XYZ-{tok}-QWE") == f"경기도 {tok}"  # 가평군 — ASCII 필러라 tok만 토큰 매칭


# ── 2026-11-15 운영 보관장소 3,277곳 점검으로 고친 것(eval/results/region_fix.md) ──
import pytest  # noqa: E402

from apps.api.region import RegionResolver as _RR, region_matches  # noqa: E402
from apps.api.config import settings as _settings  # noqa: E402

_RULES = _RR(_settings.data_dir)
_RULES.geocoded = {}  # 규칙만 시험(장소 검색 캐시 없이)


@pytest.mark.parametrize("dep, want", [
    ("광주송정역(한국철도공사)", "광주광역시"),        # '광주'가 경기도 광주시로만 등록돼 있었음(562건)
    ("광주공항", "광주광역시"),
    ("CGV(광주첨단점)", "광주광역시"),
    ("광주경찰서", "경기도 광주시"),                   # 경기 광주 — 경찰서 사전
    ("경기광주역(한국철도공사)", "경기도 광주시"),
    ("봉화산역(6호선)", "서울특별시 중랑구"),           # '봉화'(경북)가 아님
    ("월드컵경기장역(6호선)", "서울특별시 마포구"),     # '기장'(부산)이 아님
    ("다대포항역(부산교통공사)", "부산광역시 사하구"),  # '포항'이 아님
    ("거제역(부산교통공사)", "부산광역시 연제구"),
    ("다이소(대전가수원점)", "대전광역시"),             # '수원'이 낱말 속에 묻힘 → 앞의 '대전'
    ("다이소(대구칠곡3지구점)", "대구광역시"),          # 대구 앞에 붙은 칠곡은 대구
    ("다이소(부산해운대장산본점)", "부산광역시 해운대구"),  # 같은 시도면 그대로 자세히
    ("동대구역(한국철도공사)", "대구광역시"),           # 방향 접두어(동·서·남·북·신)는 같은 지명
    ("다이소(천안성정2호점)", "충청남도 천안시 서북구"),  # '안성'이 아님
])
def test_short_place_names_only_count_at_word_start(dep, want):
    assert _RULES.resolve(dep) == want


def test_line_names_are_not_places():
    """'신분당선'·'수인분당선' 속 '분당'은 지명이 아님(양재·광교·수서역이 성남 분당으로 잡혔음) — 장소 검색 캐시로 풀림."""
    assert _RULES.resolve("광교역(신분당선)") == ""
    assert resolver.resolve("광교역(신분당선)") == "경기도 수원시 영통구"      # 커밋된 캐시(eval/data/dep_geocoded.json)
    assert resolver.resolve("신분당선 유실물센터(양재역)") == "서울특별시 서초구"


@pytest.mark.parametrize("raw, want", [
    ("서울시 동대문구", "서울특별시 동대문구"),
    ("인천 미추홀구", "인천광역시 미추홀구"),
    ("강원도 강릉시", "강원특별자치도 강릉시"),
    ("전라북도 전주시 완산구", "전북특별자치도 전주시 완산구"),
    ("경산남도 김해시", "경상남도 김해시"),                 # 사전의 오타
    ("경기도 화성시 봉담읍 와우로15번길", "경기도 화성시"),  # 시도 뒤로는 시·군·구까지만
    ("양천구", "서울특별시 양천구"),
    ("전남광주통합특별시 광산구", "광주광역시 광산구"),     # 카카오 주소의 새 이름 → 색인 다수 표기
])
def test_region_spelling_is_unified(raw, want):
    assert _RULES.canonical(raw) == want


def test_sido_less_dictionary_values_fall_back_to_place_search():
    """사전에 시도 없이 도로명 조각으로 들어간 값(과천경찰서 → '과천시 관문로')은 장소 검색 캐시로 채움."""
    assert resolver.resolve("과천경찰서") == "경기도 과천시"
    assert all(resolver.has_sido(resolver.resolve(d)) for d in ("부개파출소", "봉담지구대", "8호선(동구릉역)"))


def test_ambiguous_district_uses_metro_named_in_place():
    assert _RULES.canonical("강서구", "강서구청역(부산교통공사)") == "부산광역시 강서구"
    assert _RULES.canonical("동구", "아무개") == "동구"                     # 못 정하면 그대로


def test_unified_names_still_match_region_sets():
    assert region_matches("강원특별자치도 강릉시", ["강릉"]) and region_matches("광주광역시", ["광주"])
    assert region_matches("광주광역시 광산구", ["광주 광산"]) and not region_matches("경기도 광주시", ["광주 광산"])
