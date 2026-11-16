"""판정 후보 12개 vs 8개 비교 — 운영 코드 그대로(추출·지역·검색어·판정), 검색만 평가용 코퍼스(~1,900건)로.

질문: 판정에 넘기는 후보를 12개(MERGED_TOPK)에서 8개로 줄이면
  ① 정답이 9~12위에 있다가 빠지는 일이 얼마나 있나  ② 최종 순위·'유력(80+)' 판정이 달라지나  ③ 판정 시간·토큰이 얼마나 줄나.

- 추출: apps.agent.extract.extract(운영 프롬프트·모델). 오늘 날짜는 코퍼스 시점(2026-08-31)으로 고정 — 골든 질의가 그때 기준.
- 지역: hard 셋은 골든의 region_set·loss_date(장소를 말했다고 가정), 나머지는 운영 route.resolver.expand(place_context).
- 검색어: apps.agent.fanout.build_queries → 검색어마다 운영과 같은 점수 — kNN(lucene cosinesimil: (1+cos)/2) 상위 100 →
  지역 가점 +0.05 · 분실일 전날 습득 -0.02 · 분실일 하루 전부터만 → 상위 10 → 검색어끼리 최고점으로 합쳐 상위 12.
- 판정: apps.agent.graph.grade_candidates(외국어면 번역 동시 호출·언어 검사까지 운영과 같음)를 K=12, K=8로 각각.
  같은 신고에서 두 번 부르므로 순서를 번갈아(짝수 12 먼저, 홀수 8 먼저) 캐시·연결 효과를 상쇄.
- 한계: 운영 색인은 51만 건이라 실제 검색 순위는 이 평가보다 더 나쁠 수 있음(정답이 9~12위로 밀리는 일이 더 잦을 수 있음).

실제 Gemini 호출(123문항 × 추출·지역·판정 2회, 약 $1). 결과는 문항마다 eval/results/topk_raw.jsonl에 쌓여 끊겨도 이어서 돎.
실행:  uv run python eval/run_topk.py            # 측정(이어서)
       uv run python eval/run_topk.py --report   # 요약 → eval/results/topk_8_vs_12.md
"""

from __future__ import annotations

import json
import operator
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from apps.agent import extract as ex_mod  # noqa: E402
from apps.agent import fanout, graph, route  # noqa: E402
from apps.api import gemini  # noqa: E402
from apps.api import search as search_mod  # noqa: E402
from apps.api.region import resolver as region_resolver  # noqa: E402

CORPUS = ROOT / "data" / "corpus.json"
CACHE = ROOT / "data" / "emb_cache.json"
RAW = ROOT / "results" / "topk_raw.jsonl"
REPORT = ROOT / "results" / "topk_8_vs_12.md"
TODAY = "2026-08-31"
KS = (12, 8)
STRONG = 80
LANG = {"en": "en", "ja": "ja", "zh": "zh-CN"}

# ── 평가 시점 고정 + 토큰 계량 가로채기(운영 DB 통계에 쓰지 않음) ──
ex_mod._today = lambda: TODAY
_grade = gemini.grade
gemini.grade = lambda q, c, lang="ko", lost_date=None, today=None: _grade(q, c, lang, lost_date=lost_date, today=TODAY)
USAGE = {"calls": 0, "in": 0, "out": 0}


def _capture(res: dict) -> None:
    u = res.get("usageMetadata") if isinstance(res, dict) else None
    if u:
        USAGE["calls"] += 1
        USAGE["in"] += u.get("promptTokenCount", 0)
        USAGE["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)


gemini._meter = _capture


def load_cases() -> list[dict]:
    g = json.loads((ROOT / "golden" / "golden.json").read_text(encoding="utf-8"))["queries"]
    h = json.loads((ROOT / "golden" / "golden_hard.json").read_text(encoding="utf-8"))["cases"]
    x = json.loads((ROOT / "golden" / "golden_xlang.json").read_text(encoding="utf-8"))["queries"]
    expected = {q["id"]: q["expected"] for q in g}
    cases = [{"id": q["id"], "set": "기본", "lang": "ko", "text": q["query"], "expected": q["expected"]} for q in g]
    cases += [{"id": c["id"], "set": "어려움", "lang": "ko", "text": c["query"], "expected": c["expected"],
               "region_set": c.get("region_set") or [], "loss_date": c.get("loss_date")} for c in h]
    for q in x:
        for k, lang in LANG.items():
            if q.get(k):
                cases.append({"id": f"{q['id']}-{k}", "set": "외국어", "lang": lang, "text": q[k], "expected": expected[q["id"]]})
    return cases


def norm(v: list[float]) -> list[float]:
    n = sum(a * a for a in v) ** 0.5 or 1.0
    return [a / n for a in v]


def dot(a: list[float], b: list[float]) -> float:
    return sum(map(operator.mul, a, b))


class Index:
    """평가용 코퍼스를 운영 검색과 같은 점수로."""

    def __init__(self) -> None:
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        self.docs = [d for d in corpus if d["atcId"] in cache]
        self.vecs = [norm(cache[d["atcId"]]) for d in self.docs]
        self.region = [region_resolver.resolve(d.get("dep", "")) for d in self.docs]

    def search(self, q: str, region_set: list[str], lost_date: str | None, size: int = 10, k: int = 100) -> list[tuple[int, float]]:
        qv = norm(gemini.embed_query(q))
        floor = search_mod.date_floor(lost_date, 1, today=date.fromisoformat(TODAY))
        base = [(i, (1 + dot(qv, v)) / 2) for i, v in enumerate(self.vecs) if not floor or self.docs[i]["ymd"] >= floor]
        base.sort(key=lambda x: -x[1])
        out = []
        for i, s in base[:k]:
            if region_set and region_resolver.match(self.region[i], region_set):
                s += 0.05
            s -= search_mod.date_penalty(self.docs[i]["ymd"], lost_date, 0.02)
            out.append((i, s))
        out.sort(key=lambda x: -x[1])
        return out[:size]

    def cand(self, i: int, score: float) -> dict:
        d = self.docs[i]
        return {"atc_id": d["atcId"], "fd_sn": d.get("fdSn", "1"), "name": d["name"], "subject": d.get("subj", ""),
                "category": d.get("cat", ""), "color": d.get("color", ""), "description": "", "dep_place": d.get("dep", ""),
                "found_place": "", "region": self.region[i], "found_at": d["ymd"], "score": round(score, 4)}


def grade_once(text: str, cands: list[dict], lang: str, lost_date: str | None, k: int, tgt: str) -> dict:
    cs = [dict(c) for c in cands[:k]]
    for key in USAGE:
        USAGE[key] = 0
    t0 = time.perf_counter()
    out = graph.grade_candidates(text, cs, lang, lost_date=lost_date)
    dt = time.perf_counter() - t0
    order = [c["atc_id"] for c in out]
    grades = {c["atc_id"]: c.get("grade") for c in out}
    return {"rank": order.index(tgt) + 1 if tgt in order else None, "grade": grades.get(tgt),
            "wrong_strong": sum(1 for c in out if c["atc_id"] != tgt and (c.get("grade") or 0) >= STRONG),
            "graded": sum(1 for c in out if c.get("grade") is not None), "sec": round(dt, 2), **{f"tok_{k2}": v for k2, v in USAGE.items()}}


def measure() -> None:
    done = set()
    if RAW.exists():
        done = {json.loads(line)["id"] for line in RAW.read_text(encoding="utf-8").splitlines() if line.strip()}
    cases = [c for c in load_cases() if c["id"] not in done]
    print(f"남은 문항 {len(cases)} (이미 {len(done)})", flush=True)
    idx = Index()
    for n, c in enumerate(cases):
        try:
            ex = ex_mod.extract(c["text"], c.get("loss_date"))
            region_set = c.get("region_set") or route.resolver.expand(ex.get("place_context", ""))
            lost_date = ex.get("lost_date") or None
            merged: dict[int, float] = {}
            for q in fanout.build_queries(ex):
                for i, s in idx.search(q, region_set, lost_date):
                    merged[i] = max(s, merged.get(i, -9))
            ranked = sorted(merged.items(), key=lambda x: -x[1])
            ids = [idx.docs[i]["atcId"] for i, _ in ranked]
            search_rank = ids.index(c["expected"]) + 1 if c["expected"] in ids else None
            cands = [idx.cand(i, s) for i, s in ranked[:12]]
            order = KS if n % 2 == 0 else tuple(reversed(KS))
            res = {k: grade_once(c["text"], cands, c["lang"], lost_date, k, c["expected"]) for k in order}
            row = {**{k: c[k] for k in ("id", "set", "lang", "text")}, "place": ex.get("place_context"),
                   "regions": len(region_set), "queries": fanout.build_queries(ex), "lost_date": lost_date,
                   "search_rank": search_rank, "k12": res[12], "k8": res[8]}
        except Exception as e:  # noqa: BLE001 — 한 문항 실패로 전체가 멈추지 않게(다음 실행 때 다시)
            print(f"  ! {c['id']} 실패: {type(e).__name__}: {e}", flush=True)
            continue
        with RAW.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{n + 1:3}/{len(cases)} {c['id']:8} 검색 {search_rank or '-':>2}위 | 12개: {row['k12']['rank'] or '-'}위 "
              f"{row['k12']['sec']}s | 8개: {row['k8']['rank'] or '-'}위 {row['k8']['sec']}s", flush=True)


def report() -> None:
    rows = [json.loads(line) for line in RAW.read_text(encoding="utf-8").splitlines() if line.strip()]
    price_in, price_out = 0.75, 3.75

    def summ(rs: list[dict], k: str) -> dict:
        n = len(rs)
        ranks = [r[k]["rank"] for r in rs]
        return {
            "n": n,
            "R@1": sum(1 for x in ranks if x == 1) / n, "R@3": sum(1 for x in ranks if x and x <= 3) / n,
            "후보 안": sum(1 for x in ranks if x) / n, "MRR": sum(1 / x for x in ranks if x) / n,
            "정답 유력": sum(1 for r in rs if (r[k]["grade"] or 0) >= STRONG) / n,
            "오답 유력/건": sum(r[k]["wrong_strong"] for r in rs) / n,
            "판정 초(중앙)": statistics.median(r[k]["sec"] for r in rs), "판정 초(평균)": statistics.mean(r[k]["sec"] for r in rs),
            "입력 토큰": statistics.mean(r[k]["tok_in"] for r in rs), "출력 토큰": statistics.mean(r[k]["tok_out"] for r in rs),
            "$/건": statistics.mean((r[k]["tok_in"] * price_in + r[k]["tok_out"] * price_out) / 1e6 for r in rs),
        }

    lines = ["# 판정 후보 12개 vs 8개", "",
             f"측정 {len(rows)}문항(기본 29 · 어려움 7 · 외국어 87). 운영 코드 그대로, 검색만 평가용 코퍼스 ~1,900건 — `eval/run_topk.py`.",
             "판정 초·토큰·$는 판정 단계(외국어면 번역 동시 호출·언어 검사 포함)만. 단가 $0.75/$3.75 per 1M(2027-01부터 2배 예정).", ""]
    cols = ["n", "R@1", "R@3", "후보 안", "MRR", "정답 유력", "오답 유력/건", "판정 초(중앙)", "판정 초(평균)", "입력 토큰", "출력 토큰", "$/건"]
    lines += ["| 세트 | K | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 2)]
    for name in ("전체", "기본", "어려움", "외국어"):
        rs = rows if name == "전체" else [r for r in rows if r["set"] == name]
        if not rs:
            continue
        for k in ("k12", "k8"):
            s = summ(rs, k)
            cells = [f"{s[c]:.3f}" if isinstance(s[c], float) and c not in ("판정 초(중앙)", "판정 초(평균)", "입력 토큰", "출력 토큰", "$/건", "오답 유력/건")
                     else (f"{s[c]:.2f}" if c in ("판정 초(중앙)", "판정 초(평균)", "오답 유력/건") else
                           (f"{s[c]:,.0f}" if c in ("입력 토큰", "출력 토큰") else (f"{s[c]:.4f}" if c == "$/건" else str(s[c]))))
                     for c in cols]
            lines.append(f"| {name} | {k[1:]} | " + " | ".join(cells) + " |")
    sr = [r["search_rank"] for r in rows]
    lines += ["", "## 검색 단계(판정 전) 정답 순위", "",
              f"- 1~8위: {sum(1 for x in sr if x and x <= 8)} · 9~12위: {sum(1 for x in sr if x and 9 <= x <= 12)} · "
              f"13위 밖(검색어 3개 상위 10 합친 목록 안): {sum(1 for x in sr if x and x > 12)} · 목록에 없음: {sum(1 for x in sr if not x)}", ""]
    diff = [r for r in rows if r["k12"]["rank"] != r["k8"]["rank"]]
    lines += ["## 12개와 8개 결과가 다른 문항", "", "| 문항 | 세트 | 검색 순위 | 12개 최종 | 8개 최종 | 12개 정답 점수 | 8개 정답 점수 | 신고 |", "|---|---|---|---|---|---|---|---|"]
    for r in diff:
        f = lambda v: "-" if v is None else str(v)
        lines.append(f"| {r['id']} | {r['set']} | {f(r['search_rank'])} | {f(r['k12']['rank'])} | {f(r['k8']['rank'])} | "
                     f"{f(r['k12']['grade'])} | {f(r['k8']['grade'])} | {r['text'][:40]} |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    report() if "--report" in sys.argv else measure()
