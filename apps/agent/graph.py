"""매칭 에이전트 (LangGraph) — 신고 자연어 → 매칭 결과.

extract → route → fanout(검색어 만들기, 기본은 한 문장) → search → grade(상위 settings.grade_topk개).
노드는 평범한 함수, LangGraph가 그래프로 엮음.
v1은 선형. 추후 분기/루프(예: grade 낮으면 fanout 확장 후 재검색), DB 저장, 지속 재매칭 추가.
"""

from __future__ import annotations

import base64
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from apps.agent import extract as extract_mod
from apps.agent import fanout
from apps.agent import route
from apps.api import gemini
from apps.api.config import settings
from apps.api import search as search_mod

SEARCH_PER_QUERY = 10  # 예전 방식(fanout)에서 검색어마다 가져오는 수. 판정 후보 수는 settings.grade_topk


class AgentState(TypedDict, total=False):
    text: str
    lost_date: str
    lang: str
    extracted: dict
    region_set: list
    queries: list
    candidates: list
    matches: list


def n_extract(state: AgentState) -> dict:
    return {"extracted": extract_mod.extract(state["text"], state.get("lost_date") or None)}


def n_route(state: AgentState) -> dict:
    return {"region_set": route.resolver.expand(state["extracted"].get("place_context", ""))}


def n_fanout(state: AgentState) -> dict:
    return {"queries": fanout.build_queries(state["extracted"])}


def cand_key(it: dict) -> str:
    """습득물 후보 식별 키(atc_id_fd_sn)."""
    return f"{it.get('atc_id')}_{it.get('fd_sn')}"


def graded_keys(matches: list[dict]) -> list[str]:
    """실제로 LLM 판정이 붙은 후보 키 — 판정이 실패(전부 None)했으면 빈 목록(다음에 다시 판정)."""
    return [cand_key(m) for m in matches if m.get("grade") is not None]


def _search_queries(state: AgentState) -> list[str]:
    """이번 검색에 쓸 검색어. 기본(full)은 추출 필드로 매번 다시 만듦 — 예전 방식으로 저장된 신고(검색어 3개)도
    밤 재매칭부터 새 방식으로 찾게. 추출 칸이 다 비면 저장된 첫 검색어, 그것도 없으면 신고 문장."""
    if settings.search_query_mode == "fanout":
        return state.get("queries") or []
    qs = fanout.build_queries(state.get("extracted") or {})
    if qs:
        return qs
    stored = state.get("queries") or []
    text = (state.get("text") or "").strip()
    return stored[:1] or ([text[:200]] if text else [])


def n_search(state: AgentState) -> dict:
    ex = state["extracted"]
    region_set = state.get("region_set") or []
    lost_date = ex.get("lost_date") or None
    queries = _search_queries(state)
    topk = settings.grade_topk
    per = topk if len(queries) == 1 else SEARCH_PER_QUERY
    merged: dict[str, dict] = {}
    for q in queries:
        for it in search_mod.search(
            q, region_set=region_set, lost_date=lost_date, do_grade=False,
            size=per, candidates=100,
        ):
            key = cand_key(it)
            if key not in merged or it["score"] > merged[key]["score"]:
                merged[key] = it
    cands = sorted(merged.values(), key=lambda x: -x["score"])[:topk]
    return {"candidates": cands}


def grade_candidates(text: str, cands: list[dict], lang: str = "ko", lost_date: str | None = None) -> list[dict]:
    """후보 판정 + (외국어면) 물품명·특이사항 번역 → 결과 언어 검사 → 점수순 정렬. cands를 제자리 주석해 돌려줌.

    - 판정(점수·근거)과 번역(실제 원문)을 **동시에** 두 호출로 — 기다리는 시간은 늘지 않음.
      예전엔 판정 한 번에 이름·설명 번역까지 시켜 일본어·베트남어에서 이름이 영어, 근거가 한국어로 섞였음(11/15 측정).
    - 번역 결과는 기존 칸 이름 그대로 name_en·desc_en(= 신고자 언어 — 화면·메일·저장 후보가 이 이름을 씀).
    - 검사에 걸린 칸은 한 번만 다시 쓰고, 그래도 아니면 비움 → 물품명·설명은 한국어 원문 표시, 근거는 생략.
    """
    if not cands:
        return []
    if lang == "ko":
        grades, tr = gemini.grade(text, cands, lang, lost_date=lost_date), {}
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(gemini.grade, text, cands, lang, lost_date=lost_date)
            ft = ex.submit(gemini.translate_items, cands, lang)
            grades, tr = fg.result(), _safe(ft)
    gmap = {int(x["id"]): x for x in grades if "id" in x}
    vals: dict[str, str] = {}
    for i, it in enumerate(cands):
        g = gmap.get(i)
        it["grade"] = g.get("score") if g else None
        it["reason"] = g.get("reason") if g else None
        for src, dst in (("name", "name_en"), ("desc", "desc_en")):
            it.pop(dst, None)
            if (tr.get(i) or {}).get(src):
                it[dst] = tr[i][src]
        for k in ("name_en", "desc_en", "reason"):
            if it.get(k):
                vals[f"{k}:{i}"] = it[k]
    if lang != "ko" and vals:
        fixed = gemini.fix_language(vals, lang)
        for key in vals:
            k, i = key.split(":")
            it = cands[int(i)]
            if key in fixed:
                it[k] = fixed[key]
            elif k == "reason":
                it["reason"] = None          # 근거 생략(다른 언어로 보여 주지 않음)
            else:
                it.pop(k, None)              # 물품명·설명 → 한국어 원문 표시
    cands.sort(key=lambda it: -(it["grade"] if it["grade"] is not None else -1))
    return cands


def _safe(fut) -> dict:
    """번역 실패가 판정 결과를 막지 않게 — 실패면 빈 번역(원문 표시)."""
    try:
        return fut.result() or {}
    except Exception as e:  # noqa: BLE001
        print(f"[grade] 물품명 번역 실패({type(e).__name__}) → 원문 표시")
        return {}


def _lost_date(state: AgentState) -> str | None:
    """사용자가 고른 분실일 우선, 없으면 글에서 추출한 날짜."""
    return state.get("lost_date") or (state.get("extracted") or {}).get("lost_date") or None


def n_grade(state: AgentState) -> dict:
    cands = state.get("candidates") or []
    return {"matches": grade_candidates(state["text"], cands, state.get("lang", "ko"), lost_date=_lost_date(state))}


def _valid_gate(state: AgentState) -> str:
    """분실물 신고면 계속, 아니면(일반 질문·잡담 등) 즉시 종료 — 임베딩·검색·grade 토큰 절약."""
    return "route" if state["extracted"].get("is_lost_report", True) else END


def _build():
    g = StateGraph(AgentState)
    g.add_node("extract", n_extract)
    g.add_node("route", n_route)
    g.add_node("fanout", n_fanout)
    g.add_node("search", n_search)
    g.add_node("grade", n_grade)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", _valid_gate, {"route": "route", END: END})
    g.add_edge("route", "fanout")
    g.add_edge("fanout", "search")
    g.add_edge("search", "grade")
    g.add_edge("grade", END)
    return g.compile()


GRAPH = _build()


def run(text: str, lost_date: str | None = None, lang: str = "ko") -> dict:
    return GRAPH.invoke({"text": text, "lost_date": lost_date or "", "lang": lang})


def stream(text: str, lost_date: str | None = None, lang: str = "ko"):
    """단계별 진행 스트림 — 노드가 끝날 때마다 (단계명, 그 단계가 채운 상태)를, 마지막에 ('done', 최종 상태).

    화면이 '추출 → 지역 → 검색어 → 검색 → 판정'을 실시간으로 그리도록. 분실물 신고가 아니면 extract 뒤 바로 done.
    """
    state: AgentState = {"text": text, "lost_date": lost_date or "", "lang": lang}
    for upd in GRAPH.stream(state, stream_mode="updates"):
        for node, part in upd.items():
            state.update(part or {})
            yield node, part or {}
    yield "done", state


def _synth_text(ex: dict, note: str = "") -> str:
    """추출 필드 → 사람이 읽을 한 줄(설명/grade/저장/표시용). 사진 신고는 문장이 없으므로 합성."""
    core = " ".join(x for x in (ex.get("color", ""), ex.get("brand", ""), ex.get("item", "")) if x)
    parts = [p for p in (core, ex.get("features", ""), note.strip()) if p]
    return " / ".join(parts) or (ex.get("category", "") or "사진 신고")


def _valid_img(u: str) -> bool:
    return bool(u) and u.startswith("http") and "no_img" not in u


def _fetch_image_b64(url: str, max_bytes: int = 5_000_000) -> tuple[str | None, str]:
    """습득물 사진 URL → (base64, mime). 실패·과대·비이미지면 (None, "").

    경찰청 습득물 사진 주소만 받음(미리보기와 같은 허용 목록) — 색인 데이터의 주소가 오염돼도 서버가 엉뚱한 곳(내부망 등)에
    요청하지 않게. 다른 곳으로 넘겨져도(리디렉션) 도착 주소를 다시 확인(외부 리뷰 지적, 11/15).
    """
    from apps.api import thumbs

    if not thumbs.allowed(url):
        return None, ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if not thumbs.allowed(r.geturl()):
                return None, ""
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            data = r.read(max_bytes + 1)
        if len(data) > max_bytes or not ctype.startswith("image/"):
            return None, ""
        return base64.b64encode(data).decode(), ctype
    except Exception:  # noqa: BLE001
        return None, ""


def _visual_compare(user_b64: str, user_mime: str, matches: list[dict],
                    lang: str = "ko", top: int = 3) -> None:
    """사진 있는 상위 후보를 사용자 사진과 직접 대조(VISUAL 레인). matches를 제자리 주석.

    사진 없는 습득물이 많으므로(≈78%) 사진 있는 후보만, 지연·비용 위해 top개로 제한. 실패는 조용히 스킵.
    사진 받기·대조를 **동시에** 실행(예전엔 순차라 대조 3번 ≈ 6초 추가) — 받기 실패 대비 2배수를 받아 순위 순서로 처음 성공한 top장만 대조.
    """
    picks = [m for m in matches if _valid_img(m.get("image_url", ""))][: top * 2]
    if not picks:
        return
    with ThreadPoolExecutor(max_workers=len(picks)) as ex:
        fetched = list(ex.map(lambda m: _fetch_image_b64(m["image_url"]), picks))
    ok = [(m, b64, mime) for m, (b64, mime) in zip(picks, fetched) if b64][:top]
    if not ok:
        return

    def compare(item):
        m, cand_b64, cand_mime = item
        res = gemini.compare_images(user_b64, cand_b64, user_mime, cand_mime or "image/jpeg", lang)
        if res:
            m["visual_score"] = res.get("score")
            m["visual_reason"] = res.get("reason")

    with ThreadPoolExecutor(max_workers=len(ok)) as ex:
        list(ex.map(compare, ok))


def stream_image(image_b64: str, mime: str = "image/jpeg",
                 note: str = "", lost_date: str | None = None, lang: str = "ko"):
    """사진 신고 단계별 스트림 — extract(Vision) → route → fanout → search → grade → visual → ('done', 최종 상태).

    사진에는 문장이 없으므로 추출 필드로 검색용 text를 합성해 grade/저장/표시에 재사용.
    VISUAL 레인: 텍스트로 추린 후보 중 사진 있는 상위 몇 개를 사용자 사진과 직접 비교(가점 아님, 주석·표시).
    """
    ex = extract_mod.extract_image(image_b64, mime, note=note, lost_date=lost_date)
    state: AgentState = {"text": _synth_text(ex, note), "extracted": ex, "lang": lang}
    yield "extract", {"extracted": ex, "text": state["text"]}
    for name, node in (("route", n_route), ("fanout", n_fanout), ("search", n_search), ("grade", n_grade)):
        part = node(state)
        state.update(part)
        yield name, part
    _visual_compare(image_b64, mime, state.get("matches", []), lang)
    yield "visual", {"matches": state.get("matches", [])}
    yield "done", state


def run_image(image_b64: str, mime: str = "image/jpeg",
              note: str = "", lost_date: str | None = None, lang: str = "ko") -> dict:
    """사진(+선택 메모) 신고 → 최종 상태(단계 스트림을 끝까지 소비)."""
    state: dict = {}
    for step, part in stream_image(image_b64, mime, note=note, lost_date=lost_date, lang=lang):
        if step == "done":
            state = part
    return state


def match_stored(text: str, extracted: dict, region_set: list, queries: list,
                 lang: str = "ko", skip_keys: set[str] | None = None) -> list[dict] | None:
    """저장된 분실물 재매칭 — 추출·라우팅 생략(이미 있음), 검색+grade만.
    지속 재매칭이 open 항목마다 호출(LLM 추출/라우팅 재호출 없이 저렴). 판정 근거는 신고자 언어(lang)로.

    skip_keys: 지난번에 이미 판정한 후보 키. 이번 검색 후보가 전부 그 안에 있으면(새 습득물이 상위에 없음)
    판정할 새 정보가 없으므로 LLM 판정을 건너뛰고 None 반환 — 재매칭 비용이 '새 후보가 들어온 신고' 수에만 비례."""
    state: AgentState = {"text": text, "extracted": extracted,
                         "region_set": region_set, "queries": queries, "lang": lang}
    state.update(n_search(state))
    if skip_keys is not None and all(cand_key(c) in skip_keys for c in state.get("candidates") or []):
        return None
    state.update(n_grade(state))
    return state.get("matches", [])
