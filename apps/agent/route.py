"""RouteResolver — 장소/교통 맥락 → 지역집합(구/시). 인터페이스로 분리.

v1: LLM 세계지식 스텁(노선 API 없이 동작, 신뢰도 낮음 → HITL 확인 전제, soft 가점이라 틀려도 배제 X).
추후: 지하철/버스/기차 노선 API를 같은 인터페이스에 주입 → 에이전트 코드 무변경, 정확도만 상승.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from apps.api import gemini

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


resolver: RouteResolver = LLMRouteResolver()
