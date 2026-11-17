"""외국어 신고 검색 개선(FIND-114) 측정 — 고치기 전에 집합을 고정하고, 같은 조건으로 전·후를 건별로 견줌.

세 다리(Muse 검토 11/17):
  (a) 실측 — 경찰청 실제 분실 신고 중 AI 판정 strong이 나온 건(판정 고친 뒤 46 + 고치기 전 41, 합집합으로 고정).
      같은 신고의 한국어 원문(2회 — 추출 흔들림 = 잡음)과 AI 번역 영·중·일을 넣고, 판정 전 '검색' 단계에서
      그 습득물이 몇 위인지(8위 안이어야 판정 후보). 운영 색인 읽기만(OPENSEARCH_URL = 터널).
  (b) 회귀 — 기존 123문항(기본 29·어려움 7·번역 29×3, 전부 합성·평가용 코퍼스 ~1,900건). 운영 코드 경로 그대로.
  (c) 실제 외국인 공개 글 — 서비스가 신고로 받은 11건(원문 그대로). 정답은 12번 캐리어 하나뿐 → 건별로만 봄.

  uv run python -m eval.run_langfix freeze                    # data/lost112/frozen.json (한 번만)
  OPENSEARCH_URL=... uv run python -m eval.run_langfix rank --tag before    # (a)+(c)
  uv run python -m eval.run_langfix golden --tag before       # (b)
  uv run python -m eval.run_langfix compare before after      # 건별 표 + 미리 정한 반영 기준 → eval/results/langfix.md
  uv run python -m eval.run_langfix grade --tag old           # 3번: 실측 300건·실제 글의 후보를 지금 판정으로 다시 채점
  uv run python -m eval.run_langfix compare_grade old new     # → eval/results/langfix_grade.md

반영 기준(시험 전에 정함, 11/17): 영어·중국어 각각 고친 건 − 망가진 건 ≥ 3, 50위 밖 늘지 않음 /
한국어(2회 각각) 8위 안 하락 ≤ 1 / (b) 한국어 1위 하락 ≤ 2·8위 안 하락 ≤ 1 / (c) 12번 캐리어 1위 유지.
원자료(글·번역·순위)는 data/lost112/(저장소 밖). 결과 문서엔 관리번호·순위만.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval.run_lost112 import OUT, RAW, _load

FROZEN = RAW / "frozen.json"
LANGS = ("en", "zh-CN", "ja")
CARRIER = "F2026071300001923_1"  # 실제 외국인 글 12번 — 마포경찰서 보관 캐리어


# ── 집합 고정 ───────────────────────────────────────────────────────
def _strong_keys(judge_path: Path, srch: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for j in _load(judge_path):
        for k, v in enumerate(j.get("verdicts") or []):
            if v.get("v") == "strong":
                m = srch[j["atcId"]]["matches"][k]
                out.setdefault(j["atcId"], set()).add(f"{m['atc_id']}_{m['fd_sn']}")
    return out


def freeze() -> None:
    if FROZEN.exists():
        raise SystemExit(f"이미 고정됨: {FROZEN} — 집합은 바꾸지 않음")
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    tr = {(x["atcId"], x["lang"]): x["text"] for x in _load(RAW / "lang.jsonl") if x.get("text")}
    v2 = _strong_keys(RAW / "judge.jsonl", srch)
    v1 = _strong_keys(RAW / "judge_v1_truncated.jsonl", srch)
    cases = []
    for i in sorted(set(v1) | set(v2)):
        texts = {"ko": srch[i]["text"], **{lg: tr.get((i, lg), "") for lg in LANGS}}
        cases.append({"id": i, "lost_date": rows[i].get("lstYmd"), "in46": i in v2, "in41": i in v1,
                      "wanted": sorted(v1.get(i, set()) | v2.get(i, set())), "texts": texts})
    posts = []
    res = {o["n"]: o for o in _load(RAW / "real_posts" / "results.jsonl")}
    src = _load(RAW / "real_posts" / "verified.jsonl")
    for n, o in sorted(res.items()):
        if o.get("is_lost_report", True):
            posts.append({"id": f"v{n}", "lang": o["lang"], "lost_date": o["lost_used"], "text": src[n - 1]["text"],
                          "wanted": [CARRIER] if n == 12 else []})
    r2 = _load(RAW / "real_posts" / "round2.jsonl")
    for n in (1, 3):  # 2번(유튜브 설명 문구)은 제외 — Muse 트리아지
        c = r2[n - 1]
        posts.append({"id": f"r{n}", "lang": c["lang"], "lost_date": c["lost_date"], "text": c["text"], "wanted": []})
    missing = [(c["id"], lg) for c in cases for lg in LANGS if not c["texts"][lg]]
    FROZEN.write_text(json.dumps({"made": f"{dt.datetime.now():%Y-%m-%d %H:%M}", "cases": cases, "posts": posts,
                                  "missing_translation": missing}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"고정: 실측 {len(cases)}건(46건 {sum(c['in46'] for c in cases)} · 41건 {sum(c['in41'] for c in cases)}), "
          f"실제 글 {len(posts)}건, 번역 빠짐 {len(missing)}")


# ── 실측·실제 글 순위 ───────────────────────────────────────────────
def _rank_one(job: tuple) -> dict:
    from apps.agent import graph
    from apps.api import search as search_mod

    cid, lang, rep, text, lost_date, wanted = job
    st: dict = {"text": text, "lost_date": lost_date or "", "lang": lang}
    try:
        st.update(graph.n_extract(st))
        st.update(graph.n_route(st))
        qs = graph._search_queries(st)
        hits = search_mod.search(qs[0], region_set=st.get("region_set") or [],
                                 lost_date=st["extracted"].get("lost_date") or None,
                                 do_grade=False, size=50, candidates=100) if qs else []
    except Exception as e:  # noqa: BLE001
        return {"id": cid, "lang": lang, "rep": rep, "error": f"{type(e).__name__}: {e}"[:200]}
    keys = [f"{h['atc_id']}_{h['fd_sn']}" for h in hits]
    ex = st["extracted"]
    if ex.get("degraded"):  # AI 추출 실패(429 등) — 11/17처럼 조용히 깨진 결과를 비교에 섞지 않게
        return {"id": cid, "lang": lang, "rep": rep, "error": "추출 실패(degraded)"}
    return {"id": cid, "lang": lang, "rep": rep, "query": qs[0] if qs else "",
            "rank": next((n + 1 for n, k in enumerate(keys) if k in set(wanted)), None),
            "top8": keys[:8], "region": (st.get("region_set") or [])[:6],
            "ex": {k: ex.get(k) for k in ("item", "brand", "color", "features", "place_context", "place_ko")}}


def rank(tag: str, workers: int) -> None:
    fz = json.loads(FROZEN.read_text(encoding="utf-8"))
    jobs = []
    for c in fz["cases"]:
        for lang, rep in (("ko", 1), ("ko", 2), *((lg, 1) for lg in LANGS)):
            if c["texts"].get(lang):
                jobs.append((c["id"], lang, rep, c["texts"][lang], c["lost_date"], c["wanted"]))
    for p in fz["posts"]:
        jobs.append((p["id"], p["lang"], 1, p["text"], p["lost_date"], p["wanted"]))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(_rank_one, jobs))
    p = RAW / f"rank_{tag}.jsonl"
    p.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out), encoding="utf-8")
    print(f"{p.name}: {len(out)}건, 오류 {sum(1 for o in out if o.get('error'))}")


# ── (b) 123문항 회귀 — 운영 코드 경로(추출→지역→검색어)로, 평가용 코퍼스에서 ─────────
def golden(tag: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import run_merge as M  # noqa: E402
    import run_topk as T  # noqa: E402
    from apps.agent import graph

    idx = T.Index()

    def one(c: dict) -> dict:
        st: dict = {"text": c["text"], "lost_date": c.get("loss_date") or "", "lang": c["lang"]}
        st.update(graph.n_extract(st))
        if st["extracted"].get("degraded"):
            return {"id": c["id"], "set": c["set"], "lang": c["lang"], "error": "추출 실패(degraded)", "rank": None}
        st["region_set"] = c.get("region_set") or graph.n_route(st)["region_set"]
        qs = graph._search_queries(st) or [c["text"]]
        v = T.norm(T.gemini.embed_texts(qs[:1])[0])
        ids = [idx.docs[i]["atcId"] for i, _ in M.search_vec(idx, v, st["region_set"], st["extracted"].get("lost_date") or None)[:30]]
        return {"id": c["id"], "set": c["set"], "lang": c["lang"], "query": qs[0],
                "rank": ids.index(c["expected"]) + 1 if c["expected"] in ids else None}

    with ThreadPoolExecutor(max_workers=6) as ex:
        out = list(ex.map(one, T.load_cases()))
    p = RAW / f"golden_{tag}.jsonl"
    p.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out), encoding="utf-8")
    print(f"{p.name}: {len(out)}문항, 추출 실패 {sum(1 for o in out if o.get('error'))}")


# ── 3번: 판정 과신 — 같은 후보를 같은 때 옛·새 판정으로 다시 채점 ─────────────────
def _grade_jobs() -> list[dict]:
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    jobs = [{"id": s["atcId"], "kind": "lost112", "text": s["text"], "lang": "ko", "lost_date": rows[s["atcId"]].get("lstYmd"),
             "cands": s["matches"]} for s in _load(RAW / "search.jsonl") if s.get("matches")]
    for o in _load(RAW / "real_posts" / "results.jsonl"):
        if o.get("matches"):
            src = _load(RAW / "real_posts" / "verified.jsonl")[o["n"] - 1]
            jobs.append({"id": f"v{o['n']}", "kind": "post", "text": src["text"], "lang": o["lang"],
                         "lost_date": o["lost_used"], "cands": o["matches"]})
    r2 = _load(RAW / "real_posts" / "round2.jsonl")
    for o in _load(RAW / "real_posts" / "round2_results.jsonl"):
        if o.get("matches"):
            c = r2[o["n"] - 1]
            jobs.append({"id": f"r{o['n']}", "kind": "post", "text": c["text"], "lang": c["lang"],
                         "lost_date": c["lost_date"], "cands": o["matches"]})
    return jobs


def grade(tag: str, workers: int) -> None:
    from apps.agent.rematch import alert_level
    from apps.api import gemini

    def one(j: dict) -> dict:
        cands = [dict(c) for c in j["cands"]]
        res = {int(x["id"]): x for x in gemini.grade(j["text"], cands, j["lang"], lost_date=j["lost_date"])}
        out = []
        for i, c in enumerate(cands):
            g = res.get(i) or {}
            m = {**c, "grade": g.get("score"), "link_place": g.get("place"), "link_date": g.get("date")}
            try:
                alert = alert_level(g.get("score"), False, m) == "strong"
            except TypeError:  # 옛 코드(알림 조건 없음)
                alert = alert_level(g.get("score"), False) == "strong"
            out.append({"key": f"{c['atc_id']}_{c['fd_sn']}", "score": g.get("score"), "place": g.get("place"),
                        "date": g.get("date"), "alert": alert})
        if all(g["score"] is None for g in out):
            return {"id": j["id"], "kind": j["kind"], "grades": out, "error": "판정 실패"}
        return {"id": j["id"], "kind": j["kind"], "grades": out}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(one, _grade_jobs()))
    p = RAW / f"grade_{tag}.jsonl"
    p.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in out), encoding="utf-8")
    print(f"{p.name}: {len(out)}건, 판정 실패 {sum(1 for o in out if all(g['score'] is None for g in o['grades']))}")


def compare_grade(a: str, b: str) -> None:
    """반영 기준(시험 전, 11/17): 'AI 판정 none인데 유력 알림 대상' 후보 30% 이상 감소, 'strong인데 알림에서 빠짐' ≤ 1."""
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    verdict = {}
    for j in _load(RAW / "judge.jsonl"):
        for k, v in enumerate(j.get("verdicts") or []):
            m = srch[j["atcId"]]["matches"][k]
            verdict[(j["atcId"], f"{m['atc_id']}_{m['fd_sn']}")] = v.get("v")
    A = {o["id"]: o for o in _load(RAW / f"grade_{a}.jsonl") if not o.get("error")}
    B = {o["id"]: o for o in _load(RAW / f"grade_{b}.jsonl") if not o.get("error")}
    fp = {a: 0, b: 0}
    tp = {a: 0, b: 0}
    lost_tp = gained_tp = 0
    top_fp = {a: 0, b: 0}
    top_tp = {a: 0, b: 0}
    for rid, oa in A.items():
        if oa["kind"] != "lost112" or rid not in B:
            continue
        ga, gb = {g["key"]: g for g in oa["grades"]}, {g["key"]: g for g in B[rid]["grades"]}
        for key in ga:
            v = verdict.get((rid, key))
            for tag, G in ((a, ga), (b, gb)):
                if G.get(key, {}).get("alert"):
                    fp[tag] += v == "none"
                    tp[tag] += v == "strong"
            if v == "strong":
                lost_tp += bool(ga[key]["alert"]) and not gb.get(key, {}).get("alert")
                gained_tp += (not ga[key]["alert"]) and bool(gb.get(key, {}).get("alert"))
        for tag, G in ((a, ga), (b, gb)):  # 실제 알림은 점수 1위 하나 — 신고 단위로도
            top = max(G.values(), key=lambda g: g["score"] if g["score"] is not None else -1)
            if top.get("alert"):
                v = verdict.get((rid, top["key"]))
                top_fp[tag] += v == "none"
                top_tp[tag] += v == "strong"
    lines = [f"# 판정 과신 — {a} → {b}", "", "실측 300건의 후보 8개씩(같은 후보·같은 때 다시 채점). 'AI 판정' = 다른 모델의 strong/none.", "",
             "| | 전 | 후 |", "|---|---|---|",
             f"| 후보 단위: 유력 알림 대상인데 AI 판정 none | {fp[a]} | {fp[b]} |",
             f"| 후보 단위: 유력 알림 대상이고 AI 판정 strong | {tp[a]} | {tp[b]} |",
             f"| 신고 단위(점수 1위만 알림): 잘못된 유력 알림 | {top_fp[a]} | {top_fp[b]} |",
             f"| 신고 단위: strong 후보로 유력 알림 | {top_tp[a]} | {top_tp[b]} |",
             f"| strong인데 알림에서 빠짐 / 새로 알림 대상 | - | {lost_tp} / {gained_tp} |", "",
             "## 실제 외국인 글", "", "| 글 | 점수 1위 전→후 | 알림 전→후 | 새 판정 장소·날짜(1위) |", "|---|---|---|---|"]
    for rid, oa in A.items():
        if oa["kind"] != "post" or rid not in B:
            continue
        ta = max(oa["grades"], key=lambda g: g["score"] or -1)
        tb = max(B[rid]["grades"], key=lambda g: g["score"] or -1)
        car = next((g for g in B[rid]["grades"] if g["key"] == CARRIER), None)
        extra = f" · 캐리어 {next((g['score'] for g in oa['grades'] if g['key'] == CARRIER), '-')}→{car['score']}" if car else ""
        lines.append(f"| {rid} | {ta['key']} {ta['score']} → {tb['key']} {tb['score']} | {ta['alert']} → {tb['alert']} | "
                     f"{tb.get('place')}·{tb.get('date')}{extra} |")
    red = (fp[a] - fp[b]) / fp[a] if fp[a] else 0
    ok1, ok2 = red >= 0.30, lost_tp <= 1
    lines += ["", "## 미리 정한 반영 기준", "", "| 기준 | 통과 | 값 |", "|---|---|---|",
              f"| none인데 알림 대상 30% 이상 감소 | {'✅' if ok1 else '❌'} | {fp[a]} → {fp[b]} ({red:.0%}) |",
              f"| strong인데 알림에서 빠짐 ≤ 1 | {'✅' if ok2 else '❌'} | {lost_tp} |",
              f"\n**종합: {'통과 → 반영' if ok1 and ok2 else '미달 → 반영 안 함'}**"]
    (OUT / "langfix_grade.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ── 비교 ─────────────────────────────────────────────────────────────
def _in8(r) -> bool:
    return bool(r) and r <= 8


def compare(a: str, b: str) -> None:
    fz = json.loads(FROZEN.read_text(encoding="utf-8"))
    A = {(o["id"], o["lang"], o["rep"]): o for o in _load(RAW / f"rank_{a}.jsonl")}
    B = {(o["id"], o["lang"], o["rep"]): o for o in _load(RAW / f"rank_{b}.jsonl")}
    bad = {k for k in set(A) | set(B) if (A.get(k) or {}).get("error") or (B.get(k) or {}).get("error")}
    for k in bad:  # 한쪽이라도 실패한 짝은 양쪽에서 뺌(실패를 '못 찾음'으로 세지 않게)
        A.pop(k, None)
        B.pop(k, None)
    lines = [f"# 외국어 신고 검색 개선 — {a} → {b}", "",
             f"집합 고정 {fz['made']}: 실측 {len(fz['cases'])}건(판정 고친 뒤 46 + 고치기 전 41의 합집합), 실제 외국인 글 {len(fz['posts'])}건.",
             "순위 = 판정 전 검색 단계에서 그 습득물의 순위(8위 안이어야 판정 후보). '밖' = 50위 밖.",
             f"AI 실패로 뺀 짝: {len(bad)}개.", ""]
    verdict = []
    for subset, sel in (("46건", lambda c: c["in46"]), ("41건", lambda c: c["in41"])):
        ids = [c["id"] for c in fz["cases"] if sel(c)]
        lines += [f"## (a) 실측 {subset}", "", "| 질의 | 8위 안 전→후 | 50위 밖 전→후 | 고친 건 | 망가진 건 |", "|---|---|---|---|---|"]
        for lang, rep, name in (("ko", 1, "한국어 1회"), ("ko", 2, "한국어 2회"), ("en", 1, "영어"), ("zh-CN", 1, "중국어"), ("ja", 1, "일본어")):
            ok_ids = [i for i in ids if (i, lang, rep) in A and (i, lang, rep) in B]
            pa = [A[(i, lang, rep)] for i in ok_ids]
            pb = [B[(i, lang, rep)] for i in ok_ids]
            i8a, i8b = sum(_in8(x.get("rank")) for x in pa), sum(_in8(x.get("rank")) for x in pb)
            oa, ob = sum(1 for x in pa if not x.get("rank")), sum(1 for x in pb if not x.get("rank"))
            fixed = sum(1 for x, y in zip(pa, pb, strict=True) if not _in8(x.get("rank")) and _in8(y.get("rank")))
            broken = sum(1 for x, y in zip(pa, pb, strict=True) if _in8(x.get("rank")) and not _in8(y.get("rank")))
            lines.append(f"| {name} | {i8a} → {i8b} | {oa} → {ob} | {fixed} | {broken} |")
            if subset == "46건":
                if lang in ("en", "zh-CN"):
                    verdict.append((f"{name}: 고친 − 망가진 ≥ 3", fixed - broken >= 3, f"{fixed} − {broken}"))
                    verdict.append((f"{name}: 50위 밖 늘지 않음", ob <= oa, f"{oa} → {ob}"))
                if lang == "ko":
                    verdict.append((f"{name}: 8위 안 하락 ≤ 1", i8a - i8b <= 1, f"{i8a} → {i8b}"))
        lines.append("")
    # 건별(46건): 순위가 8위 경계를 넘은 것만
    lines += ["### 건별 — 8위 경계를 넘나든 것(46건)", "", "| 신고 | 질의 | 전 | 후 | 전 검색어 | 후 검색어 |", "|---|---|---|---|---|---|"]
    for c in fz["cases"]:
        if not c["in46"]:
            continue
        for lang, rep in (("ko", 1), ("ko", 2), ("en", 1), ("zh-CN", 1), ("ja", 1)):
            x, y = A.get((c["id"], lang, rep), {}), B.get((c["id"], lang, rep), {})
            if _in8(x.get("rank")) != _in8(y.get("rank")):
                lines.append(f"| {c['id']} | {lang}{'#' + str(rep) if lang == 'ko' else ''} | {x.get('rank') or '밖'} | "
                             f"{y.get('rank') or '밖'} | {x.get('query', '')[:40]} | {y.get('query', '')[:40]} |")
    lines += ["", "## (c) 실제 외국인 공개 글", "", "| 글 | 언어 | 1위 전→후 | 정답 순위 전→후 | 후 검색어 | 후 지역 |", "|---|---|---|---|---|---|"]
    for p in fz["posts"]:
        x, y = A.get((p["id"], p["lang"], 1), {}), B.get((p["id"], p["lang"], 1), {})
        same = "같음" if (x.get("top8") or [None])[:1] == (y.get("top8") or [None])[:1] else "바뀜"
        want = f"{x.get('rank') or '밖'} → {y.get('rank') or '밖'}" if p["wanted"] else "-"
        lines.append(f"| {p['id']} | {p['lang']} | {same} | {want} | {y.get('query', '')[:40]} | {', '.join(y.get('region') or [])[:40]} |")
        if p["wanted"]:
            verdict.append(("(c) 캐리어 1위 유지", y.get("rank") == 1, f"{x.get('rank')} → {y.get('rank')}"))
    gA, gB = RAW / f"golden_{a}.jsonl", RAW / f"golden_{b}.jsonl"
    if gA.exists() and gB.exists():
        GA, GB = {o["id"]: o for o in _load(gA)}, {o["id"]: o for o in _load(gB)}
        for k in [k for k in GA if GA[k].get("error") or (GB.get(k) or {}).get("error")]:
            GA.pop(k, None)
            GB.pop(k, None)
        lines += ["", "## (b) 123문항 회귀(합성)", "", "| 묶음 | 1위 전→후 | 8위 안 전→후 | 1위 하락 | 8위 안 하락 |", "|---|---|---|---|---|"]
        for part, sel in (("한국어(기본·어려움)", lambda o: o["lang"] == "ko"), ("외국어(번역)", lambda o: o["lang"] != "ko")):
            ks = [k for k in GA if sel(GA[k]) and k in GB]
            t1a, t1b = sum(GA[k]["rank"] == 1 for k in ks), sum(GB[k]["rank"] == 1 for k in ks)
            i8a, i8b = sum(_in8(GA[k]["rank"]) for k in ks), sum(_in8(GB[k]["rank"]) for k in ks)
            d1 = sum(1 for k in ks if GA[k]["rank"] == 1 and GB[k]["rank"] != 1)
            d8 = sum(1 for k in ks if _in8(GA[k]["rank"]) and not _in8(GB[k]["rank"]))
            lines.append(f"| {part} | {t1a} → {t1b} | {i8a} → {i8b} | {d1} | {d8} |")
            if part.startswith("한국어"):
                verdict.append(("(b) 한국어 1위 하락 ≤ 2", d1 <= 2, str(d1)))
                verdict.append(("(b) 한국어 8위 안 하락 ≤ 1", d8 <= 1, str(d8)))
    lines += ["", "## 미리 정한 반영 기준", "", "| 기준 | 통과 | 값 |", "|---|---|---|"]
    lines += [f"| {n} | {'✅' if ok else '❌'} | {v} |" for n, ok, v in verdict]
    lines.append(f"\n**종합: {'모두 통과 → 반영' if verdict and all(ok for _, ok, _ in verdict) else '미달 → 반영 안 함'}**")
    OUT.mkdir(exist_ok=True)
    (OUT / "langfix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["freeze", "rank", "golden", "compare", "grade", "compare_grade"])
    ap.add_argument("tags", nargs="*")
    ap.add_argument("--tag", default="")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    if a.cmd == "freeze":
        freeze()
    elif a.cmd == "rank":
        rank(a.tag, a.workers)
    elif a.cmd == "golden":
        golden(a.tag)
    elif a.cmd == "grade":
        grade(a.tag, a.workers)
    elif a.cmd == "compare_grade":
        compare_grade(*a.tags)
    else:
        compare(*a.tags)


if __name__ == "__main__":
    main()
