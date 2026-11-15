"""분실일 거름 측정 — 분실일을 적으면 그 전날 이후 습득물만 찾는 규칙(apps/api/search.date_floor·date_penalty)의 효과와 위험.

오프라인(임베딩 캐시 재사용, 비용 0). 골든셋 29문항(코퍼스 2,000건 — 2026-08 습득물) + 지역 hard 7문항.
정답 습득일 F를 기준으로 분실일을 바꿔 가며 잰다:
  날짜 없음 / 맞게 적음(분실일 = F, F-1, F-2 — 잃어버린 날 또는 며칠 뒤 습득) / 하루 늦게 적음(F+1) / 이틀 늦게 적음(F+2).
한계: 코퍼스 정답 29개 중 19개가 08-31(코퍼스 마지막 날)이라 '맞게 적음'은 그날 이전 물건이 다 빠져 유리하게 나옴.
'늦게 적음'은 사용자가 날짜를 틀린 경우의 손해(정답이 걸러짐)를 보여 준다.

  uv run python -m eval.run_date
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.region import resolver, region_matches  # noqa: E402
from apps.api.search import date_floor, date_penalty  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
TODAY = date(2026, 11, 15)


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _load():
    corpus = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
    cache = json.loads((ROOT / "data" / "emb_cache.json").read_text(encoding="utf-8"))
    corpus = [it for it in corpus if it["atcId"] in cache]
    mat = [_norm(cache[it["atcId"]]) for it in corpus]
    return corpus, cache, mat


def _shift(d: str, k: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=k)).isoformat()


def rank(base: list[float], corpus, tgt: int, lost: str | None, near: float, region_set=None, w_region=0.0):
    """앱 search()와 같은 규칙: 분실일 전날 이후만 → 지역 가점 → 전날 습득 감점. 정답이 걸러지면 None."""
    floor = date_floor(lost, 1, TODAY)
    scored = []
    for i, it in enumerate(corpus):
        if floor and it["ymd"] < floor:
            continue
        s = base[i]
        if w_region and region_set and region_matches(resolver.resolve(it["dep"]), region_set):
            s += w_region
        s -= date_penalty(it["ymd"], lost, near)
        scored.append((i, s))
    scored.sort(key=lambda x: -x[1])
    return next((r for r, (i, _) in enumerate(scored, 1) if i == tgt), None)


def metrics(ranks):
    n = len(ranks)
    return {"MRR": sum(1 / r for r in ranks if r) / n, "R@1": sum(1 for r in ranks if r == 1) / n,
            "R@5": sum(1 for r in ranks if r and r <= 5) / n, "걸러짐": sum(1 for r in ranks if r is None)}


def main() -> None:
    corpus, cache, mat = _load()
    idx = {it["atcId"]: i for i, it in enumerate(corpus)}
    golden = json.loads((ROOT / "golden" / "golden.json").read_text(encoding="utf-8"))["queries"]
    qs = [(q, idx[q["expected"]], _norm(cache[f"q::{q['id']}"])) for q in golden if q["expected"] in idx]
    bases = [[sum(a * b for a, b in zip(qv, row)) for row in mat] for _, _, qv in qs]

    scen = {"날짜 없음": [None], "맞게 적음(F, F-1, F-2)": [0, -1, -2], "하루 늦게(F+1)": [1], "이틀 늦게(F+2)": [2]}
    lines = ["# 분실일 거름 측정 (골든셋 29문항, 코퍼스 2,000건)", "",
             "규칙: 분실일을 적으면 **그 전날 이후 습득물만** 찾고, 전날 습득물은 near만큼 낮춤. F = 정답 습득일.", "",
             "| 분실일 | near | MRR | R@1 | R@5 | 정답이 걸러진 질의 |", "|---|---|---|---|---|---|"]
    for name, shifts in scen.items():
        for near in ((0.0,) if shifts == [None] else (0.0, 0.01, 0.02, 0.05)):
            ranks = []
            for (q, tgt, _), base in zip(qs, bases):
                for k in shifts:
                    lost = None if k is None else _shift(corpus[tgt]["ymd"], k)
                    ranks.append(rank(base, corpus, tgt, lost, near))
            m = metrics(ranks)
            lines.append(f"| {name} | {near} | {m['MRR']:.3f} | {m['R@1']:.3f} | {m['R@5']:.3f} | "
                         f"{m['걸러짐']}/{len(ranks)} |")

    hard = json.loads((ROOT / "golden" / "golden_hard.json").read_text(encoding="utf-8"))["cases"]
    hq = [(h, idx[h["expected"]], _norm(cache[f"probe::{h['query']}"])) for h in hard
          if h["expected"] in idx and f"probe::{h['query']}" in cache]
    hb = [[sum(a * b for a, b in zip(qv, row)) for row in mat] for _, _, qv in hq]
    lines += ["", f"## 지역 hard {len(hq)}문항(지역 가점 0.05 + 케이스에 적힌 분실일)", "",
              "| 분실일 | MRR | R@1 | R@5 | 걸러짐 |", "|---|---|---|---|---|"]
    for name, use in (("안 씀", False), ("케이스 분실일로 거름", True)):
        ranks = [rank(b, corpus, tgt, h.get("loss_date") if use else None, 0.02, h.get("region_set"), 0.05)
                 for (h, tgt, _), b in zip(hq, hb)]
        m = metrics(ranks)
        lines.append(f"| {name} | {m['MRR']:.3f} | {m['R@1']:.3f} | {m['R@5']:.3f} | {m['걸러짐']}/{len(ranks)} |")
    md = "\n".join(lines)
    OUT.mkdir(exist_ok=True)
    (OUT / "date_filter.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
