"""MCP용 API(FIND-128) — AI 도구(MCP 서버 `mcp-server/lostkorea_mcp.py`)가 부르는 주소. 신고를 저장하지 않고 결과만 돌려줌.

왜: 웹 신고(POST /lost-items)는 신고를 저장하고 알림·메일까지 건다. 기관·연구용으로 실제 분실 사례를 한꺼번에 돌려 보거나
경찰 분실 신고 묶음을 일괄 매칭할 때는 저장·메일 없이 '이 신고 → 이 후보들'만 필요하다.
- `MCP_API_KEYS`(쉼표로 여러 개)가 있을 때만 열림 — 없으면 404. 헤더 `X-MCP-Key`. 키마다 하루 신고 수 상한
  (`LIMIT_MCP_REPORTS_DAY`) + 서비스 하루 AI 상한을 함께 씀(신고 1건 = 1단위).
- 신고 문장의 전화번호·카드번호 등은 AI로 보내기 전에 가림(웹 신고와 같은 apps/api/pii).
- 결과는 웹 결과 화면과 같은 정보(점수·AI 근거·장소·날짜가 이어지는지·보관 기관·전화·사진·경찰청 원본·지도 링크, 영어 칸)를 데이터로.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import pii
from .config import settings
from .desc import clean_desc
from .gemini import normalize_lang
from .limits import limiter
from .models import MAX_TEXT
from .search import _item

router = APIRouter(include_in_schema=False)
_DAY = 86400
# 한 요청 최대 신고 수·동시 실행 — 신고 1건 ~9초, Cloudflare가 100초 넘는 응답을 끊으므로 10건을 5개씩(~20초).
# 더 많이 돌릴 땐 MCP 도구가 여러 요청으로 나눔.
MAX_REPORTS = 10
WORKERS = 5
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
POLICE_URL = "https://minwon24.police.go.kr/cvlcpt/selectFindListDetail.do?cvlcptId=MW-201&pkupCmdtyMngId={}&sortSn={}"


def _key(request: Request) -> str:
    """MCP 키 확인 → 키 지문(한도 계산용). 키가 설정 안 됐으면 404(기능이 없는 것처럼), 틀리면 401."""
    keys = [k.strip() for k in (settings.mcp_api_keys or "").split(",") if k.strip()]
    if not keys:
        raise HTTPException(status_code=404, detail="Not Found")
    got = request.headers.get("x-mcp-key", "")
    if not any(hmac.compare_digest(got, k) for k in keys):
        raise HTTPException(status_code=401, detail="MCP 키가 맞지 않아요.", headers=_NO_STORE)
    return hashlib.sha256(got.encode()).hexdigest()[:12]


def _links(m: dict) -> dict:
    q = m.get("org_name") or " ".join(x for x in (m.get("region"), m.get("dep_place")) if x)
    return {
        "police_url": POLICE_URL.format(quote(m.get("atc_id", "")), quote(m.get("fd_sn") or "1")) if m.get("atc_id") else "",
        "naver_map": f"https://map.naver.com/p/search/{quote(q)}" if q else "",
        "google_map": f"https://www.google.com/maps/search/?api=1&query={quote(q)}" if q else "",
    }


def found_item(m: dict) -> dict:
    """습득물 한 건 — 웹 카드와 같은 정보(한국어 원문 + 영어 칸)."""
    note = clean_desc(m.get("description"))
    return {
        "atc_id": m.get("atc_id", ""), "fd_sn": m.get("fd_sn") or "1",
        "name": m.get("name", ""), "name_en": m.get("name_en") or m.get("name_mt") or "",
        "category": m.get("category", ""), "category_en": m.get("category_en", ""),
        "color": m.get("color", ""), "color_en": m.get("color_en", ""),
        "found_at": m.get("found_at", ""),
        "found_place_type": m.get("found_place", ""), "found_place_type_en": m.get("found_place_en", ""),
        "region": m.get("region", ""), "region_en": m.get("region_en", ""),
        "kept_at": m.get("dep_place", ""), "kept_at_en": m.get("dep_mt", ""),
        "agency": m.get("org_name", ""), "agency_en": m.get("org_mt", ""), "phone": m.get("tel", ""),
        "note": note, "note_en": m.get("desc_en") or m.get("note_mt") or "",
        "photo_url": m.get("image_url", "") if "no_img" not in (m.get("image_url") or "") else "",
        **_links(m),
    }


def _candidate(m: dict) -> dict:
    return {
        "score": m.get("grade"), "ai_reason": m.get("reason") or "",
        "place_link": m.get("link_place") or "", "date_link": m.get("link_date") or "",
        **found_item(m),
    }


def _summary(top: int | None) -> str:
    if top is not None and top >= settings.rematch_grade_threshold:
        return "strong"
    if top is not None and top >= settings.weak_grade_threshold:
        return "weak"
    return "none"


def match_one(text: str, lost_date: str | None, lang: str, top: int) -> dict:
    """신고 문장 하나 → 웹과 같은 AI 매칭(추출 → 경로 지역 → 검색 → 판정). 저장·메일 없음."""
    from apps.agent import graph as agent_graph

    clean, _ = pii.mask(text)
    res = agent_graph.run(clean, lost_date, lang)
    ex = res.get("extracted") or {}
    if not ex.get("is_lost_report", True):
        return {"is_lost_report": False, "summary": "not_a_lost_report", "extracted": ex, "candidates": []}
    matches = [m for m in res.get("matches") or [] if isinstance(m, dict)]
    best = matches[0].get("grade") if matches else None
    return {
        "is_lost_report": True, "summary": _summary(best), "top_score": best,
        "extracted": {k: ex.get(k) for k in ("item", "brand", "color", "category", "place_context", "lost_date", "features")
                      if ex.get(k)},
        "searched_regions": res.get("region_set") or [],
        "candidates": [_candidate(m) for m in matches[:top]],
    }


class McpReport(BaseModel):
    text: str = Field(min_length=2, max_length=MAX_TEXT)
    lost_date: str | None = Field(None, max_length=10)
    lang: str | None = Field(None, max_length=10)


class McpMatchReq(BaseModel):
    reports: list[McpReport] = Field(min_length=1, max_length=MAX_REPORTS)
    top: int | None = Field(None, ge=1, le=8)


@router.post("/mcp-api/match")
def mcp_match(req: McpMatchReq, request: Request) -> JSONResponse:
    """{reports: [{text, lost_date?, lang?}] (1~10), top?} → 신고마다 AI 매칭 결과(입력 순서 그대로)."""
    who = _key(request)
    items = [(r.text.strip(), (r.lost_date or "")[:10] or None, normalize_lang(r.lang or "ko")) for r in req.reports]
    top = req.top or (8 if len(items) == 1 else 3)
    for _ in items:  # 먼저 한도 확인 — 넘으면 하나도 안 돌림
        limiter.hit(f"mcpday:{who}", settings.limit_mcp_reports_day, _DAY,
                    message="이 MCP 키의 하루 신고 상한을 넘었어요.")
    limiter.spend(len(items))
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda it: match_one(it[0], it[1], it[2], top), items))
    return JSONResponse({"results": [{"input": it[0], **r} for it, r in zip(items, res, strict=True)]},
                        headers=_NO_STORE)


def _police_status(atc_id: str, fd_sn: str) -> dict:
    """경찰청 공개 API로 지금 상태(보관중·종결 등). 실패하면 status=''와 오류."""
    from apps.collector import client

    source = "portal" if atc_id.startswith("V") else "police"
    try:
        d = client.get_detail(source, atc_id, fd_sn) or {}
    except Exception as e:  # noqa: BLE001
        return {"status": "", "error": type(e).__name__}
    return {"status": d.get("csteSteNm", "") or "", "checked": bool(d)}


def _index_doc(atc_id: str, fd_sn: str) -> dict | None:
    url = f"{settings.opensearch_url}/{settings.opensearch_index}/_doc/{quote(atc_id)}_{quote(fd_sn)}?_source_excludes=embedding"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)
    except Exception:  # noqa: BLE001 — 404(색인에서 빠짐)도 여기로
        return None
    return d.get("_source") if d.get("found") else None


@router.get("/mcp-api/items/{atc_id}")
def mcp_item(atc_id: str, request: Request, fd_sn: str = "1") -> JSONResponse:
    """습득물 한 건 — 우리 색인의 내용(있으면) + 경찰 쪽 지금 상태. 색인에서 빠졌어도(종결 등) 경찰 상태는 확인."""
    who = _key(request)
    if not (atc_id[:1] in ("F", "V") and atc_id[1:].isalnum() and len(atc_id) <= 32 and fd_sn.isdigit()):
        raise HTTPException(status_code=400, detail="습득물 번호 형식이 아니에요(F… 또는 V…).", headers=_NO_STORE)
    limiter.hit(f"mcpitem:{who}", settings.limit_mcp_items_hour, 3600, message="습득물 상태 확인이 너무 많아요.")
    src = _index_doc(atc_id, fd_sn)
    item = found_item(_item(src, f"{atc_id}_{fd_sn}")) if src else {"atc_id": atc_id, "fd_sn": fd_sn,
                                                                     **_links({"atc_id": atc_id, "fd_sn": fd_sn})}
    return JSONResponse({"in_our_index": bool(src), "police": _police_status(atc_id, fd_sn), "item": item},
                        headers=_NO_STORE)
