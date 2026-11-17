"""RouteResolver — 장소/교통 맥락 → 지역집합(구/시). 인터페이스로 분리.

- 버스 번호가 노선표(eval/data/bus_routes.json, scripts/build_bus_routes.py — 서울 API + 전국 국토부 API + 대구·광주 TAGO)에 있으면
  **실제 노선이 지나는 구**를 씀.
- 그 밖(지하철·기차·장소, 표에 없는 버스)은 LLM 세계지식(Gemini)으로 추정 — soft 가점이라 틀려도 배제 X.
왜 나눴나: 버스 번호는 Gemini가 가장 약한 곳 — 세 자리 간선은 다른 도시로 헷갈리고(102번 → 부산) 모르는 노선은
구를 15~20개씩 뿌림(eval/results/route_bus.md, 서울 37개 노선: 실제 3.9개 → Gemini 10개, 정밀도 38%).
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from apps.api import gemini
from apps.api.config import settings
from apps.api.region import region_matches, short_region
from apps.api.region import resolver as region_resolver

_PROMPT = (Path(__file__).parent / "prompts" / "route.txt").read_text(encoding="utf-8")
MAX_REGIONS = 20  # 가점 대상 구역 상한(너무 넓으면 가점이 무의미)


class RouteResolver(Protocol):
    def expand(self, place_context: str) -> list[str]:
        ...


class LLMRouteResolver:
    """LLM 지식으로 노선/장소 → 경유 구/시 집합."""

    def __init__(self, cache_size: int = 2000):
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._size = cache_size
        self._lock = threading.Lock()

    def expand(self, place_context: str) -> list[str]:
        """장소 → 경유 구역(최대 MAX_REGIONS).

        - 같은 장소(공백·대소문자 무시)는 메모리 캐시 재사용 — LLM 호출 0, 결과도 매번 같음
          (실측: 같은 '홍대입구역'이 호출마다 10~20개로 달랐다). 신고 문장은 담지 않고 장소 표현만, 재시작 시 비움.
        - 너무 길면 **앞뒤를 남김**: 노선은 출발→도착 순으로 나오는데 앞에서만 자르면 도착지가 잘렸다
          (실측: '서울역→부산역 KTX'에서 부산 누락).
        """
        if not place_context:
            return []
        key = " ".join(place_context.split()).lower()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return list(self._cache[key])
        res = gemini.generate_json(_PROMPT.format(place=place_context))
        regions = res.get("regions") if isinstance(res, dict) else None
        regions = regions if isinstance(regions, list) else []
        regions = list(dict.fromkeys(r.strip() for r in regions if isinstance(r, str) and r.strip()))
        if len(regions) > MAX_REGIONS:
            half = MAX_REGIONS // 2
            regions = regions[:half] + regions[-half:]
        if regions:  # 실패(빈 결과)는 캐시하지 않음 — 다음에 다시 시도
            with self._lock:
                self._cache[key] = regions
                while len(self._cache) > self._size:
                    self._cache.popitem(last=False)
        return list(regions)


# ---------- 버스 노선표 ----------
_BUS_WORD = re.compile(r"버스|bus", re.I)
# 버스 번호 후보: 146 · N37 · 9401-1 · 강남01(마을) · 01B. 뒤에 호선·출구·날짜 단위가 붙은 숫자(2호선·3번 출구·11월)는 아님.
_BUS_NO = re.compile(r"[A-Za-z가-힣]*\d+(?:-\d+)?[A-Za-z]?(?!\d)(?!\s*(?:호선|번\s*출구|출구|번지|층|호|일|월|시|분|명|개|원|년|km))")
# 번호를 뺀 나머지가 이런 말뿐이면('146번 버스 안에서', '서울 146번') 따로 Gemini에 묻지 않음
_STOP = {"버스", "마을버스", "광역버스", "심야버스", "공항버스", "시내버스", "좌석버스", "간선버스", "지선버스", "직행버스",
         "시외버스", "리무진", "공항리무진", "bus", "no", "number", "the", "on", "in", "at", "번", "타고", "탔다", "탔는데",
         "안", "내", "근처", "정류장", "정류소", "방향", "쪽", "가는", "가다", "가던", "길", "두고", "놓고", "내림", "내렸",
         "내려서", "타서", "탄", "갈아탐", "갈아타고", "갈아탔", "환승", "잃어버림", "분실", "and", "then"}
_JOSA = re.compile(r"(에서|으로|에게|까지|부터|에|로|의|을|를|이|가|은|는)$")
_VARIANT = re.compile(r"^(\d+)-\d+$")  # 갈래 번호 '201-11' → 본 번호 '201'
_SIDO_ONLY = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)"
                        r"(특별시|광역시|특별자치시|특별자치도|도|시)?$")


def _place_words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[가-힣A-Za-z]+", text):
        w = _JOSA.sub("", w)
        if len(w) >= 2 and w.lower() not in _STOP:
            out.append(w)
    return out


def _has_place_words(text: str) -> bool:
    return any(not _SIDO_ONLY.match(w) for w in _place_words(text))


def _hint(rest: str) -> str:
    """번호를 뺀 나머지 말의 지명 → '시도 시군구'(보관장소 해석기 재사용, 메모 안 남김). 문장 통째로 → 낱말마다('번 버스 명동' → 명동)."""
    for s in [rest] + _place_words(rest):
        r = region_resolver.canonical(region_resolver.resolve_detail(s)[0], s)
        if r and region_resolver.has_sido(r):
            return r
    return ""


class BusTable:
    """노선번호 → 실제 경유 구(서울 출처 + 전국 국토부 출처 + 대구·광주 TAGO 출처). 여러 도시에 같은 번호가 있으면(서울 320 · 안산 320 ·
    부산 7 · 수원 7) 문장 속 지명으로 고르고, 지명이 없으면 서울 노선을 먼저(외국인 방문의 37%가 서울).
    서울에 없는 번호를 지명 없이 말하면(예 '7번 버스') 여러 도시라 못 고름 → Gemini."""

    def __init__(self, routes: dict | None = None):
        self.by_key: dict[str, list[dict]] = {}
        for r in (routes or {}).values():
            if r.get("key") and r.get("regions"):
                self.by_key.setdefault(r["key"], []).append(r)
        # 갈래 번호: 제주는 국토부 자료에 '201-11'·'201-12'처럼 갈래까지 붙어 오는데 사람은 '201번'이라고 함(TAGO도 '201') →
        # 같은 도시에 본 번호 노선이 따로 없으면 갈래들을 본 번호로도 찾게. 서울 '15-1'처럼 본 번호가 따로 있으면 그대로.
        have = {(r.get("city"), k) for k, rs in self.by_key.items() for r in rs}
        alias: dict[str, list[dict]] = {}
        for k, rs in self.by_key.items():
            m = _VARIANT.match(k)
            if m:
                alias.setdefault(m.group(1), []).extend(r for r in rs if (r.get("city"), m.group(1)) not in have)
        for k, rs in alias.items():
            if rs:
                self.by_key.setdefault(k, []).extend(rs)

    @classmethod
    def load(cls, path: Path) -> BusTable:
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")).get("routes", {}))
        except (OSError, ValueError):
            return cls()  # 표가 없으면 전부 Gemini(예전과 같음)

    def lookup(self, place_context: str) -> tuple[list[str], str] | None:
        """(실제 경유 구, 번호를 뺀 나머지 말) 또는 None(버스 얘기가 없거나 표에서 못 찾음 → Gemini)."""
        if not self.by_key or not place_context or not _BUS_WORD.search(place_context):
            return None
        found: list[dict] = []
        rest = place_context
        for m in _BUS_NO.finditer(place_context):
            tok = m.group()
            prefix = re.match(r"[가-힣]*", tok).group()
            if tok.upper() in self.by_key:  # '강남01'(마을버스) 같은 이름 그대로
                found += self.by_key[tok.upper()]
                rest = rest.replace(tok, " ", 1)
            elif prefix and tok[len(prefix):].upper() in self.by_key:  # '사가정역2250' → 2250번, 사가정역은 남김
                found += self.by_key[tok[len(prefix):].upper()]
                rest = rest.replace(tok, prefix + " ", 1)
        if not found:
            return None
        rest = " ".join(rest.split())
        hint = _hint(rest) if rest else ""  # '부산'·'안산'·'인천공항'·'강남역'·'명동'
        if hint:  # 문장에 지명이 있으면 그곳을 지나는 노선만 — 시군구까지 맞는 노선이 있으면 그것('수원 7번' → 수원 7번)
            sido = hint.split()[0]
            found = [r for r in found if any(region_matches(g, [sido]) for g in r["regions"])]
            if len(hint.split()) >= 2:
                found = [r for r in found if region_matches(hint, r["regions"])] or found
            if not found:
                return None  # 예: '부산 102번 버스'인데 표에 부산 102번이 없음 → Gemini
        else:
            found = [r for r in found if r.get("city") == "서울"] or found
        if len({r.get("city") for r in found}) > 3:
            return None  # 여러 도시에 흔한 번호 — 고를 근거가 없음
        regions = list(dict.fromkeys(g for r in found for g in r["regions"]))
        # 번호 밖에 적은 장소('2250번 버스, 사가정역'): 그 장소의 구만 더함(Gemini는 인접 구까지 넓혀 다시 흐려지므로).
        # 시도만 나온 곳('서울'·'인천공항' → 인천광역시)은 더하지 않음 — 시도 전체 가점은 무의미. 해석기가 못 푸는 장소일 때만 Gemini로.
        if hint:
            if len(hint.split()) >= 2 and not region_matches(hint, regions):
                regions.append(short_region(hint))
            return regions, ""
        return regions, (rest if _has_place_words(rest) else "")


class HybridRouteResolver:
    """버스 번호는 노선표, 나머지는 fallback(Gemini). 번호 밖에 적은 장소를 해석기가 못 풀 때만 그 부분을 Gemini로
    넓혀 덧붙임 — 노선표 구는 자르지 않고, 덧붙이는 쪽만 MAX_REGIONS까지."""

    def __init__(self, table: BusTable, fallback: RouteResolver):
        self.table = table
        self.fallback = fallback

    def expand(self, place_context: str) -> list[str]:
        hit = self.table.lookup(place_context)
        if hit is None:
            return self.fallback.expand(place_context)
        regions, rest = hit
        extra = self.fallback.expand(rest) if rest else []
        room = max(0, MAX_REGIONS - len(regions))
        return list(dict.fromkeys(regions + [g for g in extra if g not in regions][:room]))


resolver: RouteResolver = HybridRouteResolver(BusTable.load(settings.data_dir / "bus_routes.json"), LLMRouteResolver())
