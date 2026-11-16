"""검색어 합치는 방식 비교 — 판정(LLM) 전 검색 순위만. 정답이 상위 8·12 안에 들어오는가.

배경: 좁게·중간·넓게 3개 검색어를 '높은 점수 하나'로 합치면, 넓은 검색어가 평범한 물건에 준 높은 점수가
좁은 검색어의 정답을 밀어냄(eval/results/topk_8_vs_12.md — 좁은 검색어 1위 정답이 합친 뒤 11~15위).

비교하는 방식(모두 운영과 같은 검색 점수: kNN (1+cos)/2 상위 100 → 지역 +0.05 · 분실일 전날 -0.02 · 분실일 하루 전부터):
  지금        검색어마다 상위 10 → 같은 물건은 높은 점수 → 점수순(운영 그대로)
  좁게만      좁은 검색어 하나(검색어 목록의 첫 번째)
  전부 한 문장 브랜드·색·물건·특징을 한 검색어로(좁게 + 특징)
  좁게 먼저   좁은 결과 상위를 먼저 채우고, 남는 자리를 중간·넓게 순서로
  번갈아      좁게 1위 → 중간 1위 → 넓게 1위 → 좁게 2위 … (중복 제외)
  깎기 d      중간은 d, 넓게는 2d만큼 점수를 깎고 '지금'처럼 합침(d = 0.01·0.02·0.03·0.05)
  순위 합산   RRF(60) — 좁게 가중 2, 중간 1, 넓게 1

추출은 운영 프롬프트로 새로 함(특징 칸이 필요해서, 문항당 AI 1회 ≈ $0.001). 임베딩은 문항당 한 번에 묶어 보냄.
실행: uv run python eval/run_merge.py   → eval/results/merge_compare.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import run_topk as T  # noqa: E402  — 평가 시점 고정·검색 점수·코퍼스를 그대로 씀

OUT = ROOT / "results" / "merge_compare.md"
DEPTH = 30          # 방식마다 만드는 목록 길이(순위 계산용)
PER_QUERY = 10      # '지금' 방식의 검색어당 상위 개수(운영 SEARCH_PER_QUERY)


def search_vec(idx: T.Index, qv: list[float], region_set: list[str], lost_date: str | None, k: int = 100) -> list[tuple[int, float]]:
    """T.Index.search와 같은 점수, 단 임베딩은 미리 받은 벡터로."""
    floor = T.search_mod.date_floor(lost_date, 1, today=T.date.fromisoformat(T.TODAY))
    base = [(i, (1 + T.dot(qv, v)) / 2) for i, v in enumerate(idx.vecs) if not floor or idx.docs[i]["ymd"] >= floor]
    base.sort(key=lambda x: -x[1])
    out = []
    for i, s in base[:k]:
        if region_set and T.region_resolver.match(idx.region[i], region_set):
            s += 0.05
        s -= T.search_mod.date_penalty(idx.docs[i]["ymd"], lost_date, 0.02)
        out.append((i, s))
    out.sort(key=lambda x: -x[1])
    return out


def max_merge(lists: list[list[tuple[int, float]]], per: int, penalty: list[float]) -> list[int]:
    merged: dict[int, float] = {}
    for lst, pen in zip(lists, penalty):
        for i, s in lst[:per]:
            merged[i] = max(s - pen, merged.get(i, -9))
    return [i for i, _ in sorted(merged.items(), key=lambda x: -x[1])]


def fill_first(lists: list[list[tuple[int, float]]]) -> list[int]:
    out: list[int] = []
    for lst in lists:
        for i, _ in lst:
            if i not in out:
                out.append(i)
            if len(out) >= DEPTH:
                return out
    return out


def interleave(lists: list[list[tuple[int, float]]]) -> list[int]:
    out: list[int] = []
    for r in range(DEPTH):
        for lst in lists:
            if r < len(lst) and lst[r][0] not in out:
                out.append(lst[r][0])
    return out[:DEPTH]


def rrf(lists: list[list[tuple[int, float]]], weights: list[float], k: int = 60) -> list[int]:
    sc: dict[int, float] = {}
    for lst, w in zip(lists, weights):
        for r, (i, _) in enumerate(lst, 1):
            sc[i] = sc.get(i, 0) + w / (k + r)
    return [i for i, _ in sorted(sc.items(), key=lambda x: -x[1])]


def full_query(ex: dict) -> str:
    return " ".join(x for x in (ex.get("brand", ""), ex.get("color", ""), ex.get("item", ""), ex.get("features", "")) if x).strip()


def main() -> None:
    idx = T.Index()
    cases = T.load_cases()
    methods: dict[str, list] = {}
    detail = []
    for n, c in enumerate(cases):
        ex = T.ex_mod.extract(c["text"], c.get("loss_date"))
        region_set = c.get("region_set") or T.route.resolver.expand(ex.get("place_context", ""))
        lost_date = ex.get("lost_date") or None
        fq = full_query(ex) or c["text"]
        qs = T.fanout.build_queries(ex) or [fq]  # 추출 칸이 다 비면(운영은 후보 0건) 문장 그대로 — 방식 비교가 목적이라
        vecs = [T.norm(v) for v in T.gemini.embed_texts(qs + [fq])]
        lists = [search_vec(idx, v, region_set, lost_date) for v in vecs[:-1]]
        full = search_vec(idx, vecs[-1], region_set, lost_date)
        orders = {
            "지금": max_merge(lists, PER_QUERY, [0.0] * len(lists)),
            "좁게만": [i for i, _ in lists[0]],
            "전부 한 문장": [i for i, _ in full],
            "좁게 먼저": fill_first(lists),
            "번갈아": interleave(lists),
            **{f"깎기 {d}": max_merge(lists, PER_QUERY, [0.0, d, 2 * d][:len(lists)]) for d in (0.01, 0.02, 0.03, 0.05)},
            "순위 합산": rrf(lists, [2.0, 1.0, 1.0][:len(lists)]),
        }
        tgt = c["expected"]
        ranks = {}
        for name, order in orders.items():
            ids = [idx.docs[i]["atcId"] for i in order[:DEPTH]]
            ranks[name] = ids.index(tgt) + 1 if tgt in ids else None
            methods.setdefault(name, []).append((c["set"], ranks[name]))
        detail.append({"id": c["id"], "set": c["set"], "text": c["text"], "queries": qs, "full": fq, **ranks})
        print(f"{n + 1:3}/{len(cases)} {c['id']:8} " + " ".join(f"{k}:{v or '-'}" for k, v in ranks.items()), flush=True)

    def row(name: str, rs: list) -> str:
        n = len(rs)
        r = [x for _, x in rs]
        f = lambda k: sum(1 for x in r if x and x <= k) / n
        mrr = sum(1 / x for x in r if x) / n
        return f"| {name} | {f(1):.3f} | {f(3):.3f} | {f(8):.3f} | {f(12):.3f} | {mrr:.3f} | {sum(1 for x in r if not x)} |"

    lines = ["# 검색어 합치는 방식 비교(판정 전 검색 순위)", "",
             f"{len(cases)}문항(기본 29 · 어려움 7 · 외국어 87), 평가용 코퍼스 ~1,900건, 운영과 같은 검색 점수 — `eval/run_merge.py`.",
             "R@8·R@12 = 정답이 판정 후보 8개·12개 안에 드는 비율. '없음' = 상위 30 밖.", ""]
    for part in ("전체", "기본", "어려움", "외국어"):
        lines += [f"## {part}", "", "| 방식 | R@1 | R@3 | R@8 | R@12 | MRR | 없음 |", "|---|---|---|---|---|---|---|"]
        for name, rs in methods.items():
            sub = rs if part == "전체" else [x for x in rs if x[0] == part]
            if sub:
                lines.append(row(name, sub))
        lines.append("")
    lines += ["## 문항별 순위(지금 / 좁게만 / 좁게 먼저 / 번갈아 / 전부 한 문장)", "", "| 문항 | 지금 | 좁게만 | 좁게 먼저 | 번갈아 | 전부 한 문장 | 검색어 |", "|---|---|---|---|---|---|---|"]
    g = lambda v: "-" if v is None else str(v)
    for d in detail:
        if len({d["지금"], d["좁게만"], d["좁게 먼저"], d["번갈아"], d["전부 한 문장"]}) > 1:
            lines.append(f"| {d['id']} | {g(d['지금'])} | {g(d['좁게만'])} | {g(d['좁게 먼저'])} | {g(d['번갈아'])} | {g(d['전부 한 문장'])} | "
                         f"{' / '.join(d['queries'])} · 한 문장: {d['full']} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[: 4 + 8 * 13]))


if __name__ == "__main__":
    main()
