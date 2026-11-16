"""dep(보관장소) → 구/시 지역 해석기. gazetteer(로컬) + 지오코딩 캐시.

build_gazetteer.py / geocode_deps.py 산출물(dep_gazetteer.json, dep_geocoded.json)을 로드해
4단계 폴백 + 지오코딩 캐시로 해석. 런타임 API 호출 없음(정적 조회).
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import settings


# ---------- 보관장소 해석 보정 (2026-11-15, 운영 보관장소 3,277곳 점검) ----------
# 예전 문제: 짧은 지명을 보관장소 어디에든 들어 있기만 하면 그 지역으로 봄 → '광주'(경기도 광주시로만 등록)가 광주송정역·광주공항을,
# '포항'이 다대포항역을, '기장'이 월드컵경기장역을, '수원'이 다이소(대전가수원점)을, '수지구'가 운정호수지구대를 잡았음(약 1,900건).
_METRO = {"서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시", "광주": "광주광역시",
          "대전": "대전광역시", "울산": "울산광역시"}
# 시도 표기 통일(사전·주소에 서울시·인천·경산남도처럼 섞여 있어 둘러보기 지역 목록이 갈라졌음) — 현재 공식 이름으로
_CANON_SIDO = {
    "서울특별시": "서울특별시", "서울시": "서울특별시", "서울": "서울특별시",
    "부산광역시": "부산광역시", "부산": "부산광역시", "대구광역시": "대구광역시", "대구": "대구광역시",
    "인천광역시": "인천광역시", "인천": "인천광역시", "광주광역시": "광주광역시", "대전광역시": "대전광역시", "대전": "대전광역시",
    "울산광역시": "울산광역시", "울산": "울산광역시", "세종특별자치시": "세종특별자치시", "세종시": "세종특별자치시",
    "경기도": "경기도", "경기": "경기도", "강원도": "강원특별자치도", "강원특별자치도": "강원특별자치도", "강원": "강원특별자치도",
    "충청북도": "충청북도", "충북": "충청북도", "충청남도": "충청남도", "충남": "충청남도",
    "전라북도": "전북특별자치도", "전북특별자치도": "전북특별자치도", "전북": "전북특별자치도",
    "전라남도": "전라남도", "전남": "전라남도", "경상북도": "경상북도", "경북": "경상북도",
    "경상남도": "경상남도", "경남": "경상남도", "경산남도": "경상남도", "경남상남도": "경상남도",
    "제주특별자치도": "제주특별자치도", "제주도": "제주특별자치도", "제주": "제주특별자치도",
}
_GWANGJU_GU = {"광산구", "동구", "서구", "남구", "북구"}
_SIDO_SHORTS = tuple(_METRO) + ("경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주", "세종")
_DIR_PREFIX = set("동서남북신새")          # 동대구·서대전·남춘천·신해운대·새천안 — 앞에 붙어도 같은 지명
_NOT_PLACE = ("수인분당선", "신분당선", "분당선")  # 노선 이름 속 '분당'은 지명이 아님(양재·광교·수서역이 분당으로 잡혔음)
# 규칙으로 못 푸는 것만 — 보관장소에 이 글자가 있으면 이 지역
_OVERRIDE = {
    "봉화산역": "서울특별시 중랑구",                 # '봉화'(경북 봉화군)가 아님
    "부산김해경전철": "경상남도 김해시",             # 운영사 사무실(김해) — '부산' 접두어 규칙이 부산으로 바꾸지 않게
    "월드컵경기장역(6호선)": "서울특별시 마포구",
    "거제역(부산교통공사)": "부산광역시 연제구",       # 부산 거제동 — 경남 거제시가 아님
    "수정역(부산교통공사)": "부산광역시 동구",         # 부산 수정동 — 성남 수정구가 아님
    "다대포항역": "부산광역시 사하구",
}


_SIDO_FULL = set(_CANON_SIDO.values())


def _trim(region: str) -> str:
    """'경기도 과천시 관문로' → '경기도 과천시', '경기도 화성시 봉담읍 …' → '경기도 화성시' — 시도 뒤로는 시·군·구까지만."""
    toks = region.split()
    if not toks or toks[0] not in _SIDO_FULL:
        return region
    out = [toks[0]]
    for t in toks[1:]:
        if t[-1:] not in ("시", "군", "구"):
            break
        out.append(t)
    return " ".join(out)


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _at_word_start(text: str, i: int) -> bool:
    """text[i:]에서 시작하는 지명이 다른 낱말 속에 묻힌 게 아닌가 — 앞 글자가 한글이 아니거나(괄호·공백·처음),
    방향·새 접두어(동대구)이거나, 바로 앞이 시도 이름(다이소(부산해운대점)·대구수성구청)이면 지명으로 봄."""
    if i == 0 or not _is_hangul(text[i - 1]) or text[i - 1] in _DIR_PREFIX:
        return True
    return text[:i].endswith(_SIDO_SHORTS)


def _find_word(text: str, word: str) -> int:
    """낱말 경계에서 시작하는 첫 위치(없으면 -1)."""
    i = text.find(word)
    while i >= 0:
        if _at_word_start(text, i):
            return i
        i = text.find(word, i + 1)
    return -1


class RegionResolver:
    def __init__(self, data_dir: Path):
        gaz = json.loads((data_dir / "dep_gazetteer.json").read_text(encoding="utf-8"))
        self.stations = gaz["stations"]
        self.police = gaz["police_stations"]
        self.tokens = gaz["region_tokens"]
        names = dict(gaz.get("name_to_region", {}))
        names["광주"] = "광주광역시"  # 사전엔 경기도 광주시로만 있었음 — 경기 광주는 '경기'가 함께 있을 때만(아래)
        self.names = sorted(names.items(), key=lambda kv: -len(kv[0]))
        geo = data_dir / "dep_geocoded.json"
        self.geocoded = json.loads(geo.read_text(encoding="utf-8")) if geo.exists() else {}
        # 표기 통일용: 시도까지 갖춘 지역 전체(사전 값) → 시도 없는 '양천구'·'성남시 수정구'를 하나로 정해지면 채움
        full = {v["region"] for v in self.stations.values()} | set(self.police.values()) | set(names.values())
        self.full_regions = {self._canon_sido(r) for r in full if r and r.split()[0] in _CANON_SIDO}
        self._memo: dict[str, str] = {}

    def resolve(self, dep: str) -> str:
        if dep in self._memo:
            return self._memo[dep]
        r = self.canonical(self._resolve(dep), dep)
        if not self.has_sido(r) and self.geocoded.get(dep):  # 시도를 못 정한 것(동구·도로명 조각)은 장소 검색 결과로
            r = self.canonical(self.geocoded[dep], dep)
        self._memo[dep] = r
        return r

    @staticmethod
    def has_sido(region: str) -> bool:
        return bool(region) and region.split()[0] in _SIDO_FULL

    @staticmethod
    def _canon_sido(region: str) -> str:
        toks = region.split()
        if toks and toks[0] in _CANON_SIDO:
            toks[0] = _CANON_SIDO[toks[0]]
        return " ".join(toks)

    def canonical(self, region: str, dep: str = "") -> str:
        """지역 표기 통일 — '시도 시군구' 한 가지로, 읍·도로명 조각은 뗌. 시도가 없으면 사전에서 하나로 정해질 때만 채우고,
        여러 곳(동구·강서구 등)이면 보관장소에 적힌 광역시 이름(부산교통공사 등)으로 고름. 못 정하면 그대로."""
        return _trim(self._canonical(region, dep))

    def _canonical(self, region: str, dep: str = "") -> str:
        region = (region or "").strip()
        if not region:
            return ""
        toks = region.split()
        if toks[0] == "전남광주통합특별시" and len(toks) > 1:  # 카카오 주소의 새 이름 — 색인 대부분과 같은 표기로
            toks[0] = "광주광역시" if toks[1] in _GWANGJU_GU else "전라남도"
            return " ".join(toks)
        if toks[0] in _CANON_SIDO:
            return self._canon_sido(region)
        cands = {f for f in self.full_regions if f.endswith(" " + region)}
        if len(cands) > 1 and dep:
            hinted = {f for f in cands if any(f.startswith(_METRO[m]) for m in _METRO if _find_word(dep, m) >= 0)}
            cands = hinted or cands
        if len(cands) == 1:
            return cands.pop()
        sidos = {f.split()[0] for f in self.full_regions if toks[0] in f.split()[1:]}
        return f"{sidos.pop()} {region}" if len(sidos) == 1 else region

    def _resolve(self, dep: str) -> str:
        return self.resolve_detail(dep)[0]

    def resolve_detail(self, dep: str) -> tuple[str, str]:
        """(지역, 어떻게 풀었는지). 순서: 예외 → 지구대·경찰서 사전 → 낱말 경계의 지명 → 지오코딩 캐시 → 낱말 속 지명(예전 방식).

        낱말 속에 묻힌 지명(더현대서울·이마트해운대점·다대포항역)은 맞기도 틀리기도 해서, 실제 장소 검색(카카오) 결과가
        캐시에 있으면 그걸 먼저 씀 — scripts/build_geocode_cache.py가 이런 보관장소만 골라 채움.
        """
        if not dep:
            return "", "empty"
        for key, reg in _OVERRIDE.items():
            if key in dep:
                return reg, "override"
        if dep in self.stations:
            return self.stations[dep]["region"], "station"
        if dep.endswith("경찰서") and dep[:-3] in self.police:
            return self.police[dep[:-3]], "police"
        for suf in ("지구대", "파출소", "치안센터", "출장소"):
            if dep.endswith(suf) and dep[: -len(suf)] in self.stations:
                return self.stations[dep[: -len(suf)]]["region"], "station"
        text = dep
        for np in _NOT_PLACE:
            text = text.replace(np, " ")
        for t in self.tokens:
            i = _find_word(text, t)
            if i >= 0:
                return self._metro_wins(text, i, t), "word"
        for short, reg in self.names:
            i = _find_word(text, short)
            if i >= 0:
                if short == "광주" and "경기" in text:
                    reg = "경기도 광주시"  # 경기광주역 등
                return self._metro_wins(text, i, reg), "word"
        if self.geocoded.get(dep):
            return self.geocoded[dep], "geocoded"
        for t in self.tokens:
            if t in text:
                return t, "embedded"
        for short, reg in self.names:
            if short in text:
                return ("경기도 광주시" if short == "광주" and "경기" in text else reg), "embedded"
        return "", "none"

    def _metro_wins(self, text: str, i: int, region: str) -> str:
        """바로 앞에 광역시 이름이 붙은 지명(다이소(대구칠곡3지구점))이 다른 시도로 풀리면 그 광역시로 — 칠곡은 경북이지만
        '대구칠곡'은 대구 북구. 같은 시도면(부산해운대) 그대로."""
        for m, full in _METRO.items():
            if text[:i].endswith(m):
                resolved = self._canon_sido(region)
                if resolved.split()[0] in _CANON_SIDO.values() and not resolved.startswith(full):
                    return full
                break
        return region

    @staticmethod
    def match(region: str, region_set: list[str]) -> bool:
        return region_matches(region, region_set)


# ---------- 지역 매칭 (앱·eval 공용 — 랭킹 핵심이라 한 곳에서만 정의) ----------
# 예전엔 부분 문자열 비교('중' in '중랑구', '부산광역시 중구' → 모두 True)라 짧은 이름이 전국에 가점을 줬음.
# 이제 토큰의 '핵심 이름'(접미사 제거)끼리 같은지 비교하고, '서울 중구'처럼 시도가 붙으면 시도까지 맞아야 함.
_SIDO = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원도": "강원",
    "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
    "서울시": "서울", "전남광주통합특별시": "전남",
}
_SIDO_SHORT = set(_SIDO.values())


def _core(tok: str) -> str:
    """'서울특별시'→'서울', '전라남도'→'전남', '강남구'→'강남', '수원시'→'수원', '강남'→'강남'.

    접미사(시·군·구·도)는 3글자 이상일 때만 뗀다 — '대구'·'청도'·'영도'가 '대'·'청'·'영'이 되지 않게.
    두 글자 구('중구'·'동구')는 그대로 비교 → 시도 없는 '중' 한 글자가 전국 중구에 가점 주지 않음.
    """
    t = tok.strip()
    if t in _SIDO:
        return _SIDO[t]
    if len(t) >= 3 and t[-1] in "시군구도":
        return t[:-1]
    return t


def short_region(region: str) -> str:
    """'서울특별시 중랑구' → '서울 중랑구' (경유 지역 칩·노선표와 같은 꼴, region_matches는 어느 꼴이든 비교)."""
    toks = region.split()
    if toks and toks[0] in _SIDO:
        toks[0] = _SIDO[toks[0]]
    return " ".join(toks)


def region_matches(region: str, region_set: list[str]) -> bool:
    """해석된 지역(예 '서울특별시 중구')이 지역집합(예 ['서울 중구', '마포'])에 드는가.

    - 한 단어 항목('마포', '순천', '대구'): 해석 지역 토큰 중 핵심 이름이 같은 게 있으면 True.
    - 시도+구 항목('서울 중구'): 구가 같고, 해석 지역에 시도가 있으면 시도까지 같아야 True.
    """
    if not region:
        return False
    toks = region.split()
    cores = {_core(t) for t in toks}
    has_sido = any(_core(t) in _SIDO_SHORT for t in toks)
    for r in region_set:
        parts = (r or "").split()
        if not parts:
            continue
        if len(parts) == 1:
            if _core(parts[0]) in cores:
                return True
        elif _core(parts[-1]) in cores and (not has_sido or _core(parts[0]) in cores):
            return True
    return False


resolver = RegionResolver(settings.data_dir)
