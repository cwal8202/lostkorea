"""특이사항 레인 측정 — 내용물이 특이사항에만 적힌 습득물을 후보에 넣는 변경(search_w_desc)의 효과와 회귀.

  uv run python -m eval.run_desc_lane case     # 실제 사례(Reddit 교환학생 ARC 지갑) — 우체통 지갑 551 순위, 끈 것/켠 것
  uv run python -m eval.run_desc_lane ab       # LOST112 실제 신고 300건(run_lost112 sample) A/B — OPENSEARCH_URL 필요

A/B는 두 쪽이 같은 입력을 보게: 신고마다 추출·지역(LLM)을 **한 번만** 하고(data/lost112/desc_ab_extract.jsonl에 저장),
검색만 끈 것(w_desc=0)·켠 것(0.05)으로 두 번. 내용물 낱말이 없는 신고는 코드상 두 쪽이 같으므로 검색만 하고 넘어감.
신고 원문(특이사항 포함)에 내용물 낱말이 없으면 추출도 하지 않음 — 검색어는 원문에서 나오므로 두 쪽이 같음(생성 호출 절약).
후보 8개가 달라진 신고만 두 쪽을 각각 서비스 판정(점수순 정렬) → run_lost112와 같은 심사(strong/possible/none)로 잼.
- Gemini 실패가 결과에 섞이지 않게(_guard_gemini): 예비 모델 전환을 끄고, 하루 한도면 남은 신고를 미룸(저장 X),
  재시도 끝에 실패한 호출이 있던 신고도 저장하지 않음 → 같은 명령을 다시 돌리면 이어서.
- 색인은 읽기만. 원자료는 data/lost112/에만(저장소 X), 저장소엔 집계(eval/results/desc_lane.md)만.
- 심사 모델은 run_lost112.JUDGE_MODEL. 서비스 판정 모델은 설정(GEMINI_GRADE_MODEL)을 따름 — 같은 모델이면 결과에 적음.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from apps.api.config import settings
from eval.run_lost112 import JUDGE_MODEL, OUT, RAW, _first, _judge_one, _load, _save, to_text

W_ON = 0.05
EX_P = RAW / "desc_ab_extract.jsonl"
AB_P = RAW / "desc_ab.jsonl"
CASE = {"atc_id": "F2026110500000551", "lost_date": "2026-10-31"}
# Reddit 글을 옮겨 적지 않고 사실만 다시 씀(날짜·장소·버스·내용물). 단 '성동 집(한양대 근처)'은 원글에 없는 짐작
# ('한양대 교환학생'에서) — 결과를 읽을 때 지역이 맞춰진 조건임을 감안할 것.
CASE_TEXTS = {
    "ko": "핼러윈 밤에 홍대 근처에서 검정 지갑을 잃어버렸어요. KFC 들렀다가 심야버스 N76 타고 성동구 집(한양대 근처)으로 갔어요. "
          "현금, 외국인등록증, 한양대 학생증, 카드 몇 장이 들어 있어요.",
    "en": "Lost my black wallet on Halloween night around Hongdae. After KFC I took the N76 night bus home to Seongdong "
          "(near Hanyang Univ). It had cash, my ARC, my Hanyang University student ID and a few cards.",
}


def _key(m: dict) -> str:
    return f"{m.get('atc_id')}_{m.get('fd_sn')}"


def _search(ex: dict, region_set: list[str], w: float) -> list[dict]:
    """graph.n_search와 같은 검색(검색어 한 문장 → 후보 grade_topk개)을 w_desc만 바꿔서 — 설정을 고치지 않음(스레드 안전)."""
    from apps.agent import graph
    from apps.api import search

    qs = graph._search_queries({"extracted": ex})
    assert len(qs) <= 1, "full 방식(검색어 한 문장) 기준 측정"
    if not qs:
        return []
    res = search.search(qs[0], region_set=region_set, lost_date=ex.get("lost_date") or None,
                        size=settings.grade_topk, candidates=100, w_desc=w)
    return sorted(res, key=lambda x: -x["score"])


def _rank(cands: list[dict], atc_id: str) -> int | None:
    return next((i + 1 for i, m in enumerate(cands) if m["atc_id"] == atc_id), None)


# ── 실제 사례 ─────────────────────────────────────────────────────────
def case() -> None:
    from apps.agent import extract, fanout, route
    from apps.api import search

    lines = ["# 특이사항 레인 — 실제 사례(우체통 지갑)", "",
             f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, 운영 색인 읽기만. 찾는 습득물: {CASE['atc_id']}"
             " (성동경찰서 우체통, 이름 '지갑', 특이사항 '신용카드 1·일반카드 1·외국인등록증', 색 '기타')", "",
             "- 실제 흐름 글에는 원글에 없는 '성동 집(한양대 근처)'을 짐작으로 넣음 → 지역이 맞춰진 조건",
             f"- 추출·경유 지역은 LLM({settings.gemini_grade_model})이라 실행마다 조금씩 다름 — 영어 글의 검색어가 영어로 남는지에 따라 크게 달라짐", "",
             "| 질의 | 지역 | 끈 것(100개 중) | 켠 것 | 판정 8개 안 |", "|---|---|---|---|---|"]
    fixed = ("지갑 현금, 외국인등록증(ARC), 학생증", ["마포", "성동"])
    runs = [("정해진 질의: " + fixed[0], fixed[1], fixed[0])]
    for lang, text in CASE_TEXTS.items():
        ex = extract.extract(text, CASE["lost_date"])
        q = fanout.full_query(ex)
        runs.append((f"실제 흐름({lang}) 추출: {q}", route.resolver.expand(ex.get("place_context", "")), q))
    for label, rs, q in runs:
        ranks = []
        for w in (0.0, W_ON):
            res = search.search(q, region_set=rs, lost_date=CASE["lost_date"], size=200, w_desc=w)
            ranks.append(_rank(res, CASE["atc_id"]))
        on = ranks[1]
        lines.append(f"| {label} | {'·'.join(rs) or '-'} | {ranks[0] or '100위 밖'} | {on or '밖'} | "
                     f"{'O' if on and on <= settings.grade_topk else 'X'} |")
    (OUT / "desc_lane_case.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ── LOST112 A/B ──────────────────────────────────────────────────────
_FAIL = threading.local()      # 스레드(=신고 하나)별, 재시도 끝에 실패한 Gemini 호출 수
_QUOTA = threading.Event()     # 하루 한도에 걸림 → 남은 신고는 미룸


class _DailyQuota(RuntimeError):
    """하루 요청 한도(429) — 재시도해도 오늘은 안 풀림."""


def _guard_gemini() -> None:
    """측정 중 Gemini 실패가 결과에 섞이지 않게(ab 전용, 앱 코드는 그대로).

    - 예비 모델 전환 끔: 하루 한도면 전환 대신 _QUOTA를 세우고 바로 실패 — 판정 모델이 중간에 바뀌거나
      심사 모델(3.8)과 같아지지 않게, 관리자 알림 메일도 보내지 않게.
    - 분당 한도는 더 오래 기다림(재시도 8번, 최대 약 2분) — 무료 키는 분당 한도가 작음.
    - 재시도 끝에 실패한 호출(생성·임베딩)을 스레드별로 셈 → 그 신고는 저장하지 않음. 임베딩이 실패하면
      검색이 BM25로 바뀌어 두 쪽 비교가 틀어지므로 임베딩도 셈.
    """
    from apps.api import gemini

    if getattr(gemini, "_eval_guarded", False):
        return
    orig = gemini._post

    def _post(url, body, timeout=60, max_retry=3):
        try:
            return orig(url, body, timeout, max(max_retry, 8))
        except Exception as e:
            _FAIL.n = getattr(_FAIL, "n", 0) + 1
            if (isinstance(e, urllib.error.HTTPError) and e.code == 429 and ":generateContent" not in url
                    and gemini._daily_quota(gemini._err_text(e))):
                _QUOTA.set()             # 임베딩 하루 한도(생성 쪽은 _no_switch가 잡음)
            raise

    def _no_switch(reason: str = "gone") -> bool:
        if reason == "daily":
            _QUOTA.set()
            raise _DailyQuota("하루 요청 한도")
        return False

    gemini._post = _post
    gemini._switch_to_fallback = _no_switch
    gemini._eval_guarded = True


def _prepare(r: dict) -> dict:
    """추출·지역 한 번(LLM). 두 쪽이 같은 추출을 쓰게 저장. 실패하면 error(저장하지 않음)."""
    from apps.agent import extract, route
    from apps.api.pii import mask

    if _QUOTA.is_set():
        return {"atcId": r["atcId"], "error": "daily_quota"}
    _FAIL.n = 0
    text, _ = mask(to_text(r))
    ex = extract.extract(text, r.get("lstYmd"))
    rs = route.resolver.expand(ex.get("place_context", "")) if ex.get("is_lost_report", True) else []
    if _FAIL.n or ex.get("degraded"):
        return {"atcId": r["atcId"], "error": "gemini"}
    return {"atcId": r["atcId"], "text": text, "extracted": ex, "region_set": rs}


def _ab_one(pair: tuple[dict, dict]) -> dict:
    from apps.agent import fanout, graph
    from apps.api import search

    r, p = pair
    out = {"atcId": r["atcId"]}
    if p is None:                        # 원문에 내용물 낱말 없음 — 추출·검색 없이 같은 결과(모듈 설명 참고)
        return {**out, "same": True, "groups": [], "raw_no_content": True}
    ex, rs = p["extracted"], p["region_set"]
    if _QUOTA.is_set():
        return {**out, "deferred": True}
    if not ex.get("is_lost_report", True):
        return {**out, "skip": "not_report"}
    _FAIL.n = 0
    qs = fanout.build_queries(ex)
    groups = search.query_groups(" ".join(qs))
    out["groups"] = groups
    t0 = time.time()
    a = _search(ex, rs, 0.0)
    ta = time.time() - t0
    if _FAIL.n:                          # 임베딩 실패 → BM25로 바뀐 검색 — 저장하지 않고 다음에
        return {**out, "deferred": True}
    if not groups:                       # 코드상 두 쪽이 같음 — 검색 한 번이면 충분
        return {**out, "same": True, "keys": [_key(m) for m in a]}
    t0 = time.time()
    b = _search(ex, rs, W_ON)
    out["extra_secs"] = round(time.time() - t0 - ta, 3)
    out["lane_in"] = [_key(m) for m in b if m.get("desc_match")]
    out["clash"] = [_key(m) for m in b if m.get("id_clash")]
    if _FAIL.n:
        return {**out, "deferred": True}
    if [_key(m) for m in a] == [_key(m) for m in b]:
        return {**out, "same": True, "keys": [_key(m) for m in a]}
    arms = {}
    for name, cands in (("off", a), ("on", b)):
        graded = graph.grade_candidates(p["text"], [dict(m) for m in cands], "ko", lost_date=ex.get("lost_date"))
        if _FAIL.n or all(m.get("grade") is None for m in graded):
            return {**out, "deferred": True}   # 판정 실패 — 두 쪽을 같은 실행에서 다시
        ms = [{k: m.get(k) for k in ("atc_id", "fd_sn", "name", "category", "color", "description", "dep_place",
                                      "found_place", "region", "found_at", "grade", "desc_match", "id_clash")}
              for m in graded]
        j = _judge_one((r, {"matches": ms}))
        arms[name] = {"matches": ms, "verdicts": j.get("verdicts"), "error": j.get("error")}
    return {**out, "same": False, **arms}


def ab(workers: int) -> None:
    from apps.api import search

    _guard_gemini()
    rows = _load(RAW / "sample.jsonl")
    has = {r["atcId"] for r in rows if search.content_groups(to_text(r))}
    prep = {x["atcId"]: x for x in _load(EX_P)} if EX_P.exists() else {}
    todo = [r for r in rows if r["atcId"] in has and r["atcId"] not in prep]
    print(f"원문에 내용물 낱말이 있는 신고 {len(has)}/{len(rows)}건 — 추출·지역 {len(todo)}건, 모델 {settings.gemini_grade_model}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, x in enumerate(ex.map(_prepare, todo), 1):
            if not x.get("error"):
                prep[x["atcId"]] = x
            if i % 10 == 0 or i == len(todo):
                _save(EX_P, list(prep.values()))
                print(f"  {i}/{len(todo)}")
    done = {x["atcId"]: x for x in _load(AB_P)} if AB_P.exists() else {}

    def need(i: str) -> bool:
        d = done.get(i)
        return d is None or (not d.get("same") and not d.get("skip")
                             and bool((d.get("off") or {}).get("error") or (d.get("on") or {}).get("error")))

    todo2 = [(r, prep.get(r["atcId"])) for r in rows if need(r["atcId"])
             and (r["atcId"] not in has or r["atcId"] in prep)]
    print(f"A/B {len(todo2)}건 — 색인 {settings.opensearch_url}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, x in enumerate(ex.map(_ab_one, todo2), 1):
            if not x.get("deferred"):
                done[x["atcId"]] = x
            if i % 25 == 0 or i == len(todo2):
                _save(AB_P, [done[r["atcId"]] for r in rows if r["atcId"] in done])
                print(f"  {i}/{len(todo2)}")
    left = sum(1 for r in rows if need(r["atcId"]))
    if left:
        print(f"남은 신고 {left}건" + (" — 하루 한도. 한국 오후 5시(태평양 자정) 뒤 같은 명령으로 이어서" if _QUOTA.is_set() else
                                     " — Gemini 실패로 미룸. 같은 명령을 다시 돌리면 이어서"))
    report()


def report() -> None:
    rows = _load(RAW / "sample.jsonl")
    done = {x["atcId"]: x for x in _load(AB_P)}
    xs = [done[r["atcId"]] for r in rows if r["atcId"] in done]
    n = len(xs)
    fired = [x for x in xs if x.get("groups")]
    changed = [x for x in xs if not x.get("same") and not x.get("skip") and x.get("off") and x.get("on")
               and not x["off"].get("error") and not x["on"].get("error")]

    def first(x: dict, arm: str, want: set[str]) -> int | None:
        return _first(x[arm]["verdicts"] or [], want)

    def cnt(arm: str, pred) -> int:
        return sum(1 for x in changed if pred(first(x, arm, {"strong"})))

    lines = [
        "# 특이사항 레인 — 실제 분실 신고(LOST112) A/B", "",
        f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, run_lost112 표본 {n}건, 운영 색인 읽기만",
        "- 두 쪽이 같은 추출·지역을 씀(신고마다 LLM 한 번). 검색만 끈 것(w_desc=0)·켠 것(0.05)",
        f"- 신고 원문(특이사항 포함)에 내용물 낱말이 없는 {sum(1 for x in xs if x.get('raw_no_content'))}건은 추출 없이 같은 결과로 셈"
        " — 검색어가 원문에서 나오므로 두 쪽이 같음",
        f"- 후보 8개가 달라진 신고만 두 쪽을 각각 서비스 판정({settings.gemini_grade_model}) → 심사({JUDGE_MODEL}, run_lost112와 같은 기준)",
        "- 측정용 별도 Gemini 키(운영 한도와 분리). 예비 모델 전환은 끄고, 호출이 실패한 신고는 저장하지 않고 다시 돌림",
        "", "| 지표 | 값 |", "|---|---|",
        f"| 신고 | {n}건 |",
        f"| 내용물 낱말(외국인등록증·여권·학생증·현금 등)이 있는 신고 | {len(fired)}건 |",
        f"| └ 후보 8개가 달라진 신고 | {len(changed)}건 |",
        f"| 나머지(코드상 같은 결과) | {n - len(changed)}건 |",
    ]
    if fired:
        extra = sorted(x.get("extra_secs", 0) for x in fired if "extra_secs" in x)
        if extra:
            lines.append(f"| 검색 시간 차이(켠 것 − 끈 것, 중앙값) | {extra[len(extra) // 2]:+.2f}초 — 레인 질의는 본 검색과 동시라 잡음 수준 |")
    if changed:
        lines += ["", "## 후보가 달라진 신고", "",
                  "| | 끈 것 | 켠 것 |", "|---|---|---|",
                  f"| 8개 안에 strong이 있는 신고 | {cnt('off', bool)} | {cnt('on', bool)} |",
                  f"| └ 그중 1위 | {cnt('off', lambda r: r == 1)} | {cnt('on', lambda r: r == 1)} |",
                  f"| └ 그중 3위 안 | {cnt('off', lambda r: bool(r) and r <= 3)} | {cnt('on', lambda r: bool(r) and r <= 3)} |",
                  f"| strong 또는 possible이 있는 신고 | "
                  f"{sum(1 for x in changed if first(x, 'off', {'strong', 'possible'}))} | "
                  f"{sum(1 for x in changed if first(x, 'on', {'strong', 'possible'}))} |"]
        # 들어온 후보·밀려난 후보의 심사
        came, went = collections.Counter(), collections.Counter()
        lost_strong = []
        for x in changed:
            off = {_key(m): v["v"] for m, v in zip(x["off"]["matches"], x["off"]["verdicts"] or [], strict=False)}
            on = {_key(m): v["v"] for m, v in zip(x["on"]["matches"], x["on"]["verdicts"] or [], strict=False)}
            for k in set(on) - set(off):
                came[on[k]] += 1
            for k in set(off) - set(on):
                went[off[k]] += 1
                if off[k] == "strong":
                    lost_strong.append((x["atcId"], k))
        lines += ["", f"- 켠 것에서 새로 들어온 후보 {sum(came.values())}개의 심사: {dict(came)}",
                  f"- 밀려난 후보 {sum(went.values())}개의 심사: {dict(went)}"]
        if lost_strong:
            lines.append(f"- 밀려난 strong: {lost_strong}")
        lines += ["", "| 신고 | 내용물 | 끈 것 첫 strong | 켠 것 첫 strong | 특이사항으로 들어온 후보 |", "|---|---|---|---|---|"]
        for x in changed:
            lines.append(f"| {x['atcId']} | {'·'.join(x['groups'])} | {first(x, 'off', {'strong'}) or '-'} | "
                         f"{first(x, 'on', {'strong'}) or '-'} | {len(x.get('lane_in') or [])} |")
    if settings.gemini_grade_model == JUDGE_MODEL:
        lines += ["", f"- 주의: 서비스 판정과 심사가 같은 모델({JUDGE_MODEL}) — 3.6-flash 하루 한도 소진으로 근사."]
    (OUT / "desc_lane_ab.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ── 생성 호출 없는 검색 단계 비교 ─────────────────────────────────────
def retrieval() -> None:
    """FIND-113 실측의 추출·지역(search.jsonl)과 심사(judge.jsonl)를 다시 써서 **생성 AI 호출 없이** 검색 단계만 비교.

    저장된 추출엔 features가 없어 검색어를 '브랜드 색 물건 + 주인이 적은 물품명'으로 근사(내용물은 대개 물품명 괄호 안).
    내용물 낱말이 없는 신고는 코드상 같으므로 검색하지 않음. 임베딩은 신고당 1번(두 쪽이 같은 벡터).
    지난 심사에서 strong이던 후보가 켠 쪽 8개에서 빠지는지, 새로 들어온 후보가 무엇인지 봄 — 판정·심사는 키가 생긴 뒤 ab로.
    """
    import functools

    from apps.agent import fanout
    from apps.api import gemini, search

    orig = gemini.embed_query
    cached = functools.lru_cache(maxsize=512)(lambda q: tuple(orig(q)))
    gemini.embed_query = lambda q: list(cached(q))   # 두 쪽이 같은 벡터 + 임베딩 호출 신고당 1번
    calls = cached.cache_info
    rows = _load(RAW / "sample.jsonl")
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    jud = {j["atcId"]: j for j in _load(RAW / "judge.jsonl")}
    verdict: dict[str, str] = {}             # 지난 실측에서 심사받은 후보 → strong/possible/none(같은 신고 안)
    out, same, skipped = [], 0, 0
    for r in rows:
        s = srch.get(r["atcId"])
        if not s or s.get("error") or not s.get("is_lost_report", True):
            skipped += 1
            continue
        vs = (jud.get(r["atcId"]) or {}).get("verdicts") or []
        v_of = {_key(m): v["v"] for m, v in zip(s["matches"], vs, strict=False)}
        ex = {**s["extracted"], "features": r.get("lstPrdtNm", "")}
        q = fanout.full_query(ex)
        groups = search.query_groups(q)
        if not groups:
            same += 1
            continue
        args = {"region_set": s.get("region_set") or [], "lost_date": ex.get("lost_date") or None,
                "size": settings.grade_topk, "candidates": 100}
        a = search.search(q, w_desc=0.0, **args)
        b = search.search(q, w_desc=W_ON, **args)
        ka, kb = [_key(m) for m in a], [_key(m) for m in b]
        out.append({"atcId": r["atcId"], "q": q, "groups": groups, "regions": len(args["region_set"]),
                    "changed": ka != kb, "set_changed": set(ka) != set(kb),
                    "came": [(m["atc_id"], m["name"][:20], m.get("desc_match"), v_of.get(_key(m)))
                             for m in b if _key(m) not in ka],
                    "went": [(m["atc_id"], m["name"][:20], m.get("id_clash"), v_of.get(_key(m)))
                             for m in a if _key(m) not in kb],
                    "strong_a": [k for k in ka if v_of.get(k) == "strong"],
                    "strong_b": [k for k in kb if v_of.get(k) == "strong"],
                    "rank_strong": [(ka.index(k) + 1, kb.index(k) + 1 if k in kb else None)
                                    for k in ka if v_of.get(k) == "strong"]})
        for k in ka + kb:
            if k in v_of:
                verdict[k] = v_of[k]
    _save(RAW / "desc_retrieval.jsonl", out)
    ch = [x for x in out if x["set_changed"]]
    lost = [(x["atcId"], k) for x in out for k in x["strong_a"] if k not in x["strong_b"]]
    came = collections.Counter(str(c[3]) for x in out for c in x["came"])
    went = collections.Counter(str(c[3]) for x in out for c in x["went"])
    lines = [
        "# 특이사항 레인 — 실제 분실 신고(LOST112) 검색 단계 비교 (생성 AI 호출 없음)", "",
        f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, run_lost112 표본 {len(rows)}건, 운영 색인 읽기만",
        "- FIND-113 실측의 추출·지역을 그대로 씀. 저장된 추출에 '특징' 칸이 없어 검색어 = 브랜드·색·물건 + 주인이 적은 물품명(근사)",
        "- 같은 검색어·같은 벡터로 끈 것(w_desc=0)·켠 것(0.05)의 후보 8개 비교. 심사(strong/possible/none)는 FIND-113 실측 것을 재사용"
        " → 그때 8개 밖이던 후보는 '심사 없음'",
        "", "| 지표 | 값 |", "|---|---|",
        f"| 신고 | {len(rows)}건(신고 아님·오류 {skipped}) |",
        f"| 내용물 낱말 없음 → 코드상 같은 결과 | {same}건 |",
        f"| 내용물 낱말 있음 | {len(out)}건 |",
        f"| └ 후보 8개 구성이 바뀜 | {len(ch)}건 |",
        f"| └ 순서만 바뀜 | {sum(1 for x in out if x['changed'] and not x['set_changed'])}건 |",
        f"| 끈 쪽 8개의 strong 후보 | {sum(len(x['strong_a']) for x in out)}개 |",
        f"| └ 켠 쪽에서 8개 밖으로 밀림 | {len(lost)}개 |",
        f"| 새로 들어온 후보(지난 심사) | {sum(came.values())}개 {dict(came)} |",
        f"| 밀려난 후보(지난 심사) | {sum(went.values())}개 {dict(went)} |",
        "", "| 신고 | 검색어 | 내용물 | 지역 수 | strong 순위(끈→켠) | 들어옴(특이사항) | 밀림(어긋난 신분증) |",
        "|---|---|---|---|---|---|---|",
    ]
    for x in out:
        if not x["changed"]:
            continue
        came_s = "; ".join(f"{c[1]}({'·'.join(c[2] or []) or '-'})" for c in x["came"]) or "-"
        went_s = "; ".join(f"{c[1]}({'·'.join(c[2] or []) or '-'}, {c[3] or '심사 없음'})" for c in x["went"]) or "-"
        rs = ", ".join(f"{a}→{b or '밖'}" for a, b in x["rank_strong"]) or "-"
        lines.append(f"| {x['atcId']} | {x['q'][:40]} | {'·'.join(x['groups'])} | {x['regions']} | {rs} | {came_s} | {went_s} |")
    lines += ["", f"- 임베딩 호출 {calls().misses}번(생성 AI 호출 0)"]
    (OUT / "desc_lane.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["case", "ab", "report", "retrieval"])
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    {"case": case, "report": report, "retrieval": retrieval}.get(a.cmd, lambda: ab(a.workers))()


if __name__ == "__main__":
    main()
