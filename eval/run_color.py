"""색 모름 가점 측정 — 습득물 색 '기타'(경찰이 색을 적지 않음, 우체통 습득 대부분)를 색을 말한 신고에서 얼마나 올릴지.

  OPENSEARCH_URL=... uv run python -m eval.run_color sweep     # LOST112 실제 신고 300건, 가점 크기별 검색 단계 비교(생성 AI 0)
  OPENSEARCH_URL=... uv run python -m eval.run_color judge     # 후보가 바뀐 신고만, 들어온·나간 후보를 심사 모델로(신고당 1번)

FIND-113 실측의 추출·지역(search.jsonl)과 심사(judge.jsonl, strong/possible/none)를 다시 씀 — run_desc_lane retrieval과 같은 방식.
검색어 = 브랜드·색·물건 + 주인이 적은 물품명(저장된 추출에 '특징' 칸이 없어 근사). 색을 말하지 않은 신고는 코드상 같으므로 검색 안 함.
같은 벡터로 가점 0(끈 것)과 각 크기의 후보 8개를 견줌: 지난 심사 strong이 8개 밖으로 밀리는지, 들어오고 나간 후보가 무엇인지.
특이사항 레인은 운영과 같게 켠 채(w_desc 기본). 원자료는 data/lost112/(저장소 밖), 저장소엔 집계(eval/results/color_unknown.md)만.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import functools

from apps.api.config import settings
from eval.run_lost112 import OUT, RAW, _load, _save

WS = (0.015, 0.025, 0.035, 0.045)
# 실제 사례(한양대 교환학생 ARC 지갑, eval/results/desc_lane_case.md) — 우체통 지갑 551(색 '기타'). 정해진 질의·지역(LLM 없음)
CASE_551 = ("F2026110500000551", "2026-10-31", [
    ("정해진 질의", "지갑 현금, 외국인등록증(ARC), 학생증", ["마포", "성동"]),
    ("색 있는 질의(11/18 한국어 흐름 추출)", "검정 지갑 현금, 외국인등록증, 한양대 학생증, 카드 몇 장",
     ["서울 마포", "서울 서대문", "서울 종로", "서울 중구", "서울 용산", "서울 성동", "서울 동대문", "서울 광진", "서울 중랑",
      "서울 강북", "서울 성북"]),
])


def _key(m: dict) -> str:
    return f"{m['atc_id']}_{m.get('fd_sn', '1')}"


def sweep() -> None:
    from apps.agent import fanout
    from apps.api import gemini, search

    orig = gemini.embed_query
    cached = functools.lru_cache(maxsize=1024)(lambda q: tuple(orig(q)))
    gemini.embed_query = lambda q: list(cached(q))   # 모든 크기가 같은 벡터 + 임베딩은 검색어당 1번
    rows = _load(RAW / "sample.jsonl")
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    jud = {j["atcId"]: j for j in _load(RAW / "judge.jsonl")}
    stats = {w: collections.Counter() for w in WS}
    came_v = {w: collections.Counter() for w in WS}
    went_v = {w: collections.Counter() for w in WS}
    lost_strong = {w: [] for w in WS}
    up_strong = {w: [] for w in WS}
    nocolor = skipped = 0
    with_color = 0
    for r in rows:
        s = srch.get(r["atcId"])
        if not s or s.get("error") or not s.get("is_lost_report", True):
            skipped += 1
            continue
        ex = {**s["extracted"], "features": r.get("lstPrdtNm", "")}
        color = ex.get("color") or ""
        if not search.color_unknown_bonus("기타", color, 1.0):
            nocolor += 1
            continue
        with_color += 1
        vs = (jud.get(r["atcId"]) or {}).get("verdicts") or []
        v_of = {_key(m): v["v"] for m, v in zip(s["matches"], vs, strict=False)}
        q = fanout.full_query(ex)
        args = {"region_set": s.get("region_set") or [], "lost_date": ex.get("lost_date") or None,
                "size": settings.grade_topk, "candidates": 100, "query_color": color}
        a = search.search(q, w_color_unknown=0.0, **args)
        ka = [_key(m) for m in a]
        for w in WS:
            b = search.search(q, w_color_unknown=w, **args)
            kb = [_key(m) for m in b]
            st = stats[w]
            st["changed"] += set(ka) != set(kb)
            st["unknown_a"] += sum(1 for m in a if m.get("color") == "기타")
            st["unknown_b"] += sum(1 for m in b if m.get("color") == "기타")
            for m in b:
                if _key(m) not in ka:
                    came_v[w][f"{v_of.get(_key(m), '심사 없음')}{'·기타' if m.get('color') == '기타' else ''}"] += 1
            for m in a:
                if _key(m) not in kb:
                    went_v[w][f"{v_of.get(_key(m), '심사 없음')}{'·기타' if m.get('color') == '기타' else ''}"] += 1
            for k in ka:
                if v_of.get(k) == "strong":
                    if k not in kb:
                        lost_strong[w].append((r["atcId"], k))
                    elif kb.index(k) < ka.index(k):
                        up_strong[w].append((r["atcId"], k, ka.index(k) + 1, kb.index(k) + 1))
    # 사례 551
    case_rows = []
    gold, lost_date, qs = CASE_551
    for label, q, rs in qs:
        color = "검정" if q.startswith("검정") else ""
        ranks = []
        for w in (0.0,) + WS:
            res = search.search(q, region_set=rs, lost_date=lost_date, size=200, query_color=color, w_color_unknown=w)
            ranks.append(next((i for i, m in enumerate(res, 1) if m["atc_id"] == gold), None))
        case_rows.append((label, q, ranks))

    lines = ["# 색 모름('기타') 가점 — 실제 분실 신고(LOST112) 검색 단계 비교 (생성 AI 호출 없음)", "",
             f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, run_lost112 표본 {len(rows)}건, 운영 색인 읽기만, 특이사항 레인 켠 채",
             "- 색 '기타' = 경찰이 색을 적지 않은 습득물(우체통 습득 96%·공항 세관). 임베딩은 '기타'를 틀린 색처럼 봄",
             "- FIND-113 실측의 추출·지역 재사용, 검색어 = 브랜드·색·물건 + 주인이 적은 물품명(근사). 심사는 FIND-113 것 재사용"
             " → 그때 8개 밖이던 후보는 '심사 없음'", "",
             f"- 신고 {len(rows)}건 중 색을 말한 신고 {with_color}건(나머지 {nocolor}건은 코드상 같음, 신고 아님·오류 {skipped}건)", "",
             "| 가점 | 후보 8개가 바뀐 신고 | 8개 안 '기타' 습득물(끈 것 → 켠 것) | 지난 심사 strong이 8개 밖으로 | strong 순위 오름 |"
             " 들어온 후보(지난 심사) | 나간 후보(지난 심사) |", "|---|---|---|---|---|---|---|"]
    for w in WS:
        st = stats[w]
        lines.append(f"| {w} | {st['changed']} | {st['unknown_a']} → {st['unknown_b']} | {len(lost_strong[w])} | {len(up_strong[w])} | "
                     f"{dict(came_v[w])} | {dict(went_v[w])} |")
    lines += ["", "밀려난 strong: " + "; ".join(f"{w}: {lost_strong[w]}" for w in WS if lost_strong[w]) if any(lost_strong.values())
              else "밀려난 strong: 없음", "",
              f"## 사례 — 우체통 지갑 {gold}(색 '기타') 순위", "",
              "| 질의 | 끈 것 | " + " | ".join(str(w) for w in WS) + " |", "|---|---|" + "---|" * len(WS)]
    for label, q, ranks in case_rows:
        lines.append(f"| {label}: {q} | " + " | ".join(str(x or "밖") for x in ranks) + " |")
    lines += ["", f"- 임베딩 호출 {cached.cache_info().misses}번(생성 AI 호출 0)"]
    (OUT / "color_unknown.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


JUDGE_WS = (0.015, 0.025)
COLOR_NOTE = "기타(경찰이 색을 적지 않음)"   # 심사가 '기타'를 다른 색으로 읽지 않게 — 우체통 습득 96%(이 측정에서만)


def judge(workers: int) -> None:
    """가점 0과 JUDGE_WS 크기의 후보 8개를 다시 뽑고, 바뀐 신고마다 들어온·나간 후보를 한 번에 심사(run_lost112와 같은 심사 모델·기준).

    같은 호출 안에서 들어온 것과 나간 것을 함께 보므로 둘을 견주기 공정함. 지난 심사(FIND-113)는 쓰지 않음 — 그 뒤 색인·레인이 바뀌어
    지금 후보 대부분이 심사 없음. 색 '기타'는 '기타(경찰이 색을 적지 않음)'으로 보여 줌(심사 기준이 '색이 어긋나면 none'이라서).
    원자료 data/lost112/color_judge.jsonl, 집계 eval/results/color_unknown_judge.md.
    """
    from concurrent.futures import ThreadPoolExecutor

    from apps.agent import fanout
    from apps.api import gemini, search
    from eval.run_lost112 import JUDGE_MODEL, TOKENS, _count_tokens, _judge_cands

    orig = gemini.embed_query
    cached = functools.lru_cache(maxsize=1024)(lambda q: tuple(orig(q)))
    gemini.embed_query = lambda q: list(cached(q))
    _count_tokens()
    rows = _load(RAW / "sample.jsonl")
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    jobs = []
    for r in rows:
        s = srch.get(r["atcId"])
        if not s or s.get("error") or not s.get("is_lost_report", True):
            continue
        ex = {**s["extracted"], "features": r.get("lstPrdtNm", "")}
        color = ex.get("color") or ""
        if not search.color_unknown_bonus("기타", color, 1.0):
            continue
        q = fanout.full_query(ex)
        args = {"region_set": s.get("region_set") or [], "lost_date": ex.get("lost_date") or None,
                "size": settings.grade_topk, "candidates": 100, "query_color": color}
        a = search.search(q, w_color_unknown=0.0, **args)
        ka = {_key(m): m for m in a}
        came, went, pool = {}, {}, {}
        for w in JUDGE_WS:
            b = search.search(q, w_color_unknown=w, **args)
            kb = {_key(m): m for m in b}
            came[w] = [k for k in kb if k not in ka]
            went[w] = [k for k in ka if k not in kb]
            pool.update({k: kb[k] for k in came[w]})
            pool.update({k: ka[k] for k in went[w]})
        if pool:
            jobs.append({"r": r, "came": came, "went": went, "pool": pool})
        if cached.cache_info().misses % 50 == 0:
            print(f"  검색 {cached.cache_info().misses}건 · 바뀐 신고 {len(jobs)}", flush=True)
    print(f"후보가 바뀐 신고 {len(jobs)}건 — 심사 {JUDGE_MODEL}, 신고당 1번", flush=True)

    def one(j: dict) -> dict:
        keys = list(j["pool"])
        ms = [{**j["pool"][k], "color": COLOR_NOTE if j["pool"][k].get("color") == "기타" else j["pool"][k].get("color")}
              for k in keys]
        vs, err = _judge_cands(j["r"], ms)
        out = {"atcId": j["r"]["atcId"], "keys": keys, "colors": [j["pool"][k].get("color") for k in keys],
               "came": {str(w): v for w, v in j["came"].items()}, "went": {str(w): v for w, v in j["went"].items()}}
        return {**out, "verdicts": [v["v"] for v in vs], "why": [v["why"] for v in vs]} if vs is not None else {**out, "error": err}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, jobs))
    _save(RAW / "color_judge.jsonl", res)
    ok = [x for x in res if not x.get("error")]
    lines = ["# 색 모름('기타') 가점 — 들어온·나간 후보 심사", "",
             f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, LOST112 표본 300건 중 색을 말한 신고, 운영 색인 읽기만, 특이사항 레인 켠 채",
             f"- 가점 0과 {'·'.join(map(str, JUDGE_WS))}의 후보 8개가 바뀐 신고마다, 들어온·나간 후보를 한 호출로 심사({JUDGE_MODEL},"
             " run_lost112와 같은 기준). 습득물 색 '기타'는 '기타(경찰이 색을 적지 않음)'으로 보여 줌",
             f"- 심사 {len(res)}건(실패 {len(res) - len(ok)}), 호출 {TOKENS['calls']}번 · 입력 {TOKENS['in']:,} · 출력(생각 포함) {TOKENS['out']:,}토큰"
             f" ≈ ${TOKENS['in'] / 1e6 * 0.75 + TOKENS['out'] / 1e6 * 3.75:.2f}", "",
             "| 가점 | 바뀐 신고 | 들어온 후보 심사 | 나간 후보 심사 | strong 든 신고(들어옴 / 나감) | strong·possible 합(들어옴 − 나감) |",
             "|---|---|---|---|---|---|"]
    for w in JUDGE_WS:
        cv, gv = collections.Counter(), collections.Counter()
        gain = lose = 0
        changed = 0
        for x in ok:
            v_of = dict(zip(x["keys"], x["verdicts"], strict=True))
            c, g = x["came"][str(w)], x["went"][str(w)]
            if not (c or g):
                continue
            changed += 1
            cv.update(v_of[k] for k in c)
            gv.update(v_of[k] for k in g)
            gain += any(v_of[k] == "strong" for k in c)
            lose += any(v_of[k] == "strong" for k in g)
        good = lambda C: C["strong"] + C["possible"]  # noqa: E731
        lines.append(f"| {w} | {changed} | {dict(cv)} | {dict(gv)} | {gain} / {lose} | {good(cv) - good(gv):+d} |")
    lines += ["", "## 들어온 strong 후보(0.025)", ""]
    for x in ok:
        v_of = dict(zip(x["keys"], x["verdicts"], strict=True))
        why = dict(zip(x["keys"], x["why"], strict=True))
        for k in x["came"]["0.025"]:
            if v_of[k] == "strong":
                lines.append(f"- {x['atcId']} ← {k}: {why[k]}")
    lines += ["", "## 나간 strong 후보(0.025)", ""]
    for x in ok:
        v_of = dict(zip(x["keys"], x["verdicts"], strict=True))
        why = dict(zip(x["keys"], x["why"], strict=True))
        for k in x["went"]["0.025"]:
            if v_of[k] == "strong":
                lines.append(f"- {x['atcId']} → {k}: {why[k]}")
    (OUT / "color_unknown_judge.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sweep", "judge"])
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    sweep() if a.cmd == "sweep" else judge(a.workers)


if __name__ == "__main__":
    main()
