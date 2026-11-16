"""검색어 만들기 — 구조화 필드 → 임베딩 검색어. 규칙기반(LLM 불필요).

기본(`SEARCH_QUERY_MODE=full`): 브랜드·색·물건·특징을 검색어 **하나**로 — "닥스 검정 지갑 카드 2장".
  임베딩은 글자가 아니라 뜻으로 찾으므로, 브랜드가 없는 습득 기록("검정 반지갑")도 이 검색어와 충분히 가깝다.
예전(`fanout`): 넓게~좁게 여러 검색어("닥스 검정 지갑" → "검정 지갑" → "지갑")를 따로 찾아 높은 점수로 합침.
  검색어마다 점수의 뜻이 달라, 넓은 검색어가 평범한 물건에 준 높은 점수가 좁은 검색어의 정답을 밀어냈다
  (2026-11-16 측정: 정답 1위 72% → 한 문장 89%, eval/results/merge_compare.md). 되돌리기용으로 남겨 둠.
"""

from __future__ import annotations

from apps.api.config import settings


def full_query(ex: dict) -> str:
    """브랜드·색·물건(없으면 대분류)·특징을 한 줄로. 칸이 다 비면 빈 문자열."""
    parts = (ex.get("brand", ""), ex.get("color", ""), ex.get("item", "") or ex.get("category", ""), ex.get("features", ""))
    return " ".join(p.strip() for p in parts if p and p.strip())


def fanout_queries(ex: dict) -> list[str]:
    """예전 방식: 좁게 → 중간 → 넓게(중복 제거·순서 유지)."""
    brand, color, item, cat = ex.get("brand", ""), ex.get("color", ""), ex.get("item", ""), ex.get("category", "")
    cands = [
        " ".join(x for x in (brand, color, item) if x),   # 좁게: 닥스 검정 지갑
        " ".join(x for x in (color, item) if x),           # 중간: 검정 지갑
        item or cat,                                        # 넓게: 지갑
    ]
    out: list[str] = []
    for q in cands:
        q = q.strip()
        if q and q not in out:
            out.append(q)
    return out


def build_queries(ex: dict, mode: str | None = None) -> list[str]:
    """설정된 방식(기본 full)으로 검색어 목록. 칸이 다 비면 []."""
    if (mode or settings.search_query_mode) == "fanout":
        return fanout_queries(ex)
    q = full_query(ex)
    return [q] if q else []
