"""MCP 공통(FIND-128) — 원격 MCP(`/mcp`, apps/api/mcp_remote.py)와 내 PC용 MCP(`mcp-server/lostkorea_mcp.py`가 부르는
`/mcp-api/*`)가 함께 쓰는 확인·한도·매칭. 신고는 저장하지 않고 결과만 돌려줌.

왜: 웹 신고(POST /lost-items)는 신고를 저장하고 알림·메일까지 건다. AI가 실제 분실 사례를 한꺼번에 돌려 보거나 기관이 분실
신고 묶음을 일괄 매칭할 때는 저장·메일 없이 '이 신고 → 이 후보들'만 필요하다.
- 누가 부르나: MCP 키(운영 화면에서 기관마다 발급, DB엔 해시만)가 있으면 그 키의 하루 한도(0 = 한도 없음, 운영자용),
  없으면 누구나 — IP당 하루 AI 매칭 3건(`LIMIT_MCP_PUBLIC_DAY`), 한 번에 3건까지. 키는 `X-MCP-Key` 또는 `Authorization: Bearer`.
- 신고 문장의 전화번호·카드번호 등은 AI로 보내기 전에 가림(웹 신고와 같은 apps/api/pii).
- 결과는 웹 결과 화면과 같은 정보(점수·AI 근거·장소·날짜가 이어지는지·보관 기관·전화·사진·경찰청 원본·지도 링크, 영어 칸).
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps import store

from . import pii
from .config import settings
from .desc import clean_desc
from .gemini import normalize_lang
from .limits import client_ip, limiter
from .models import MAX_TEXT
from .search import _item

router = APIRouter(include_in_schema=False)
_DAY, _HOUR = 86400, 3600
# 한 번에 받는 신고 수·동시 실행 — 신고 1건 ~9초, Cloudflare가 100초 넘는 응답을 끊으므로 10건을 5개씩(~20초).
MAX_REPORTS = 10
PUBLIC_MAX_REPORTS = 3
WORKERS = 5
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
POLICE_URL = "https://minwon24.police.go.kr/cvlcpt/selectFindListDetail.do?cvlcptId=MW-201&pkupCmdtyMngId={}&sortSn={}"


@dataclass
class Caller:
    """부르는 쪽 — 키가 있으면 그 키(id·이름·하루 한도), 없으면 IP(누구나 한도)."""
    ip: str
    key: dict | None = None

    @property
    def label(self) -> str:
        return f"key:{self.key['id']}" if self.key else f"ip:{self.ip}"


def caller_from(headers, ip: str, require_key: bool = False) -> Caller:
    """헤더의 MCP 키 확인. 키를 냈는데 틀리면(회수 포함) 401 — 조용히 누구나 한도로 내려가지 않게."""
    raw = (headers.get("x-mcp-key") or "").strip()
    auth = headers.get("authorization") or ""
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    if raw:
        row = store.mcp_key_find(raw)
        if not row:
            raise HTTPException(status_code=401, detail="MCP 키가 맞지 않거나 회수됐어요.", headers=_NO_STORE)
        return Caller(ip=ip, key=row)
    if require_key:
        raise HTTPException(status_code=401, detail="MCP 키가 필요해요.", headers=_NO_STORE)
    return Caller(ip=ip)


def charge_reports(c: Caller, n: int) -> None:
    """신고 n건을 돌리기 전에 한도 확인·차감 — 넘으면 하나도 안 돌림(429). 키 한도 0은 한도 없음(운영자)."""
    if c.key:
        if n > MAX_REPORTS:
            raise HTTPException(status_code=400, detail=f"한 번에 {MAX_REPORTS}건까지 보내 주세요.")
        limit = int(c.key.get("daily_limit") or 0)
        if limit and store.mcp_usage_today(c.key["id"]) + n > limit:
            raise HTTPException(status_code=429, detail=f"이 키의 하루 한도({limit}건)를 넘어요. 내일 다시 쓰거나 운영자에게 문의하세요.")
        if limit:
            limiter.spend(n)  # 운영자 키(한도 0)는 서비스 하루 AI 상한도 건너뜀
        store.mcp_use(c.key["id"], n)
        store.bump("mcp_report_key", n)
        return
    if n > PUBLIC_MAX_REPORTS:
        raise HTTPException(status_code=400, detail=f"키 없이는 한 번에 {PUBLIC_MAX_REPORTS}건까지예요. 기관 키는 운영자에게 신청하세요.")
    for _ in range(n):
        limiter.hit(f"mcppub:{c.ip}", settings.limit_mcp_public_day, _DAY,
                    message=f"키 없이는 하루 {settings.limit_mcp_public_day}건까지예요. 기관 키는 help@lostkorea.com 으로 신청하세요.")
    limiter.spend(n)
    store.bump("mcp_report_public", n)


def charge_item(c: Caller) -> None:
    """습득물 상태 확인(경찰청 공개 API) — 키마다·IP마다 시간당."""
    lim = settings.limit_mcp_items_hour if c.key else settings.limit_mcp_public_items_hour
    if c.key and not int(c.key.get("daily_limit") or 0):
        return
    limiter.hit(f"mcpitem:{c.label}", lim, _HOUR, message="습득물 상태 확인이 너무 많아요. 잠시 뒤 다시 하세요.")


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


def run_matches(items: list[tuple[str, str | None, str]], top: int) -> list[dict]:
    """[(문장, 분실일, 언어)] → 결과(입력 순서 그대로), 5개씩 동시."""
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda it: match_one(it[0], it[1], it[2], top), items))
    return [{"input": it[0], **r} for it, r in zip(items, res, strict=True)]


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


def valid_item_id(atc_id: str, fd_sn: str) -> bool:
    return atc_id[:1] in ("F", "V") and atc_id[1:].isalnum() and len(atc_id) <= 32 and fd_sn.isdigit()


def item_status(atc_id: str, fd_sn: str) -> dict:
    """습득물 한 건 — 우리 색인의 내용(있으면) + 경찰 쪽 지금 상태. 색인에서 빠졌어도(종결 등) 경찰 상태는 확인."""
    src = _index_doc(atc_id, fd_sn)
    item = found_item(_item(src, f"{atc_id}_{fd_sn}")) if src else {"atc_id": atc_id, "fd_sn": fd_sn,
                                                                     **_links({"atc_id": atc_id, "fd_sn": fd_sn})}
    return {"in_our_index": bool(src), "police": _police_status(atc_id, fd_sn), "item": item}


# ── 내 PC용 MCP(mcp-server/lostkorea_mcp.py)가 부르는 주소 — 키 필수 ──
class McpReport(BaseModel):
    text: str = Field(min_length=2, max_length=MAX_TEXT)
    lost_date: str | None = Field(None, max_length=10)
    lang: str | None = Field(None, max_length=10)


class McpMatchReq(BaseModel):
    reports: list[McpReport] = Field(min_length=1, max_length=MAX_REPORTS)
    top: int | None = Field(None, ge=1, le=8)


def to_items(reports: list[McpReport]) -> list[tuple[str, str | None, str]]:
    return [(r.text.strip(), (r.lost_date or "")[:10] or None, normalize_lang(r.lang or "ko")) for r in reports]


@router.post("/mcp-api/match")
def mcp_match(req: McpMatchReq, request: Request) -> JSONResponse:
    """{reports: [{text, lost_date?, lang?}] (1~10), top?} → 신고마다 AI 매칭 결과(입력 순서 그대로)."""
    c = caller_from(request.headers, client_ip(request), require_key=True)
    items = to_items(req.reports)
    charge_reports(c, len(items))
    top = req.top or (8 if len(items) == 1 else 3)
    return JSONResponse({"results": run_matches(items, top)}, headers=_NO_STORE)


@router.get("/mcp-api/items/{atc_id}")
def mcp_item(atc_id: str, request: Request, fd_sn: str = "1") -> JSONResponse:
    c = caller_from(request.headers, client_ip(request), require_key=True)
    if not valid_item_id(atc_id, fd_sn):
        raise HTTPException(status_code=400, detail="습득물 번호 형식이 아니에요(F… 또는 V…).", headers=_NO_STORE)
    charge_item(c)
    return JSONResponse(item_status(atc_id, fd_sn), headers=_NO_STORE)
