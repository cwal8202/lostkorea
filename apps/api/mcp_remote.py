"""원격 MCP(FIND-128) — `https://lostkorea.com/mcp` 주소 하나를 AI 앱(Claude·Cursor 등)에 붙여 넣으면 바로 쓰는 MCP 서버.

웹 서버 안에서 돈다(MCP Python SDK 2.3 `MCPServer`, Streamable HTTP, 세션 없이 요청마다 JSON 응답). 도구 3개는 내 PC용
MCP(mcp-server/lostkorea_mcp.py)와 같고, 확인·한도·매칭은 apps/api/mcp_api.py를 함께 씀:
- 키 없이 누구나 — IP당 하루 AI 매칭 3건(한 번에 3건까지), 습득물 상태 확인 시간당 60건.
- 기관 키(`X-MCP-Key` 또는 `Authorization: Bearer`) — 운영 화면에서 발급한 하루 한도, 한 번에 10건.
  머리글을 못 넣는 Claude 앱 커넥터용으로 주소 `https://lostkorea.com/mcp?key=키`도 받음(main._key_from_url이 머리글로 옮김).
도구 응답에 신고를 저장하지 않음. 신고 문장의 전화번호 등은 AI 전에 가림.
"""

from __future__ import annotations

import anyio
from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import mcp_api
from . import search as search_mod
from .config import settings
from .limits import client_ip, limiter
from .models import MAX_TEXT

server = MCPServer(
    name="LostKorea",
    instructions=(
        "LostKorea matches lost-item reports against items handed in to police stations and lost-and-found centers "
        "across Korea (Korean National Police Agency open data, items found within about the last 6 months). "
        "find_matches runs reports through the AI matcher (3 per day without a key; organizations get keys from "
        "help@lostkorea.com), search_found_items browses by keyword/region/date, get_found_item checks whether an item "
        "is still held. Matches are candidates, not confirmations: the owner must confirm with the holding agency."
    ),
)


class LostReport(BaseModel):
    text: str = Field(description="What was lost, where and when, in any language (e.g. 'Black leather wallet left "
                                  "on subway line 2 near Hongdae, Nov 18'). 2-500 chars.", min_length=2, max_length=MAX_TEXT)
    lost_date: str | None = Field(None, description="Date lost, YYYY-MM-DD, if known")
    lang: str | None = Field(None, description="Language for AI reasons and translated names, e.g. 'en', 'ko', 'ja'")


def _caller(ctx: Context) -> mcp_api.Caller:
    req = getattr(ctx.request_context, "request", None)
    return mcp_api.caller_from(ctx.headers or {}, client_ip(req) if req is not None else "unknown")


def _tool_error(e: HTTPException) -> ToolError:
    """예상한 실패(한도·키·입력) — ToolError라야 AI에게 이유가 그대로 감(그 밖의 예외는 'Error executing tool'로 가려짐)."""
    return ToolError(str(e.detail))


@server.tool(title="Find matching found items for lost-item reports",
             annotations=ToolAnnotations(title="Find matches", read_only_hint=True, open_world_hint=True))
async def find_matches(reports: list[LostReport], ctx: Context, top: int | None = None) -> dict:
    """Run lost-item reports through LostKorea's AI matcher (the same one as the website) and return candidates.

    Without a key: up to 3 reports per day per person (IP), 3 per call. With an organization key: up to 10 per call
    and the key's daily limit. For each report: what the AI extracted (item, color, brand, place, date), the regions
    searched (subway/bus routes are followed into other districts), a summary ('strong' = a candidate scored 80+,
    'weak' = 50-79, 'none'), and up to `top` candidates (default 8 for one report, 3 each for several; max 8) with a
    0-100 score, the AI's reason, whether place/date line up, item details (Korean original plus English), holding
    agency and phone, photo, police listing URL and map links. Nothing is saved; phone/card numbers are masked.
    Treat results as candidates: the owner must confirm with the agency (phone or visit) before claiming.
    """
    try:
        c = _caller(ctx)
        items = mcp_api.to_items([mcp_api.McpReport(**r.model_dump()) for r in reports])
        if not items:
            raise ToolError("Send at least one report.")
        mcp_api.charge_reports(c, len(items))
    except HTTPException as e:
        raise _tool_error(e) from None
    n = max(1, min(8, top)) if top else (8 if len(items) == 1 else 3)
    results = await anyio.to_thread.run_sync(lambda: mcp_api.run_matches(items, n))
    return {"results": results}


@server.tool(title="Browse found items by keyword, region and date",
             annotations=ToolAnnotations(title="Search found items", read_only_hint=True, open_world_hint=True))
async def search_found_items(ctx: Context, keyword: str = "", region: str = "", date_from: str = "", date_to: str = "",
                             photo_only: bool = False, newest_first: bool = True, page: int = 1, size: int = 10,
                             include_similar_names: bool = False) -> dict:
    """Browse found items like the website's 'All found items' page (plain keyword search).

    keyword: item words in Korean or any language (non-Korean is translated to Korean). Spelling variants are matched
    automatically (보조밧데리 = 보조배터리); include_similar_names=True also returns items the police listed under a
    generic name (에어팟 → 무선이어폰), exact matches first. region: Korean province or 'province district' as the site
    uses (e.g. '서울특별시' or '서울특별시 강남구'). date_from/date_to: found date YYYY-MM-DD. size: 1-30.
    """
    try:
        c = _caller(ctx)
        if search_mod.needs_translation(keyword):
            limiter.hit(f"translate:{c.label}", settings.limit_translate_hour, 3600)
            limiter.spend(1)
    except HTTPException as e:
        raise _tool_error(e) from None
    d = await anyio.to_thread.run_sync(lambda: search_mod.browse(
        q=keyword[:100], region=region[:60], date_from=date_from[:10], date_to=date_to[:10], page=max(1, page),
        size=max(1, min(30, size)), photo="with" if photo_only else "", sort="new" if newest_first else "old",
        expand=include_similar_names))
    out = {"total": d.get("total"), "page": d.get("page"), "items": [mcp_api.found_item(m) for m in d.get("items") or []]}
    x = d.get("expand")
    if x and not x.get("on"):
        out["similar_names_available"] = {"generic_names": x.get("words"), "extra_items": x.get("extra"),
                                          "hint": "call again with include_similar_names=true"}
    elif x and x.get("on"):
        out["similar_names_included"] = {"generic_names": x.get("words"), "exact_matches_first": x.get("exact"),
                                         "auto": bool(x.get("auto"))}
    return out


@server.tool(title="Check one found item and its current police status",
             annotations=ToolAnnotations(title="Get found item", read_only_hint=True, open_world_hint=True))
async def get_found_item(atc_id: str, ctx: Context, fd_sn: str = "1") -> dict:
    """Look up one found item by its police ID (atc_id, e.g. 'F2026111800003998'; fd_sn usually '1').

    Returns our copy of the item (if still in the LostKorea index) and the police system's current status
    ('보관중' = still held, '종결' = closed: returned to the owner, transferred or disposed). Items leave our index when
    closed or after about 6 months, so in_our_index=false with a police status is normal.
    """
    try:
        if not mcp_api.valid_item_id(atc_id, fd_sn):
            raise ToolError("atc_id looks like 'F…' or 'V…' (letters and digits), fd_sn is a number.")
        mcp_api.charge_item(_caller(ctx))
    except HTTPException as e:
        raise _tool_error(e) from None
    return await anyio.to_thread.run_sync(lambda: mcp_api.item_status(atc_id, fd_sn))


def http_app():
    """웹 서버에 붙일 ASGI 앱(`/mcp`). 공개 주소라 DNS rebinding 보호(로컬 서버용)는 끔 — 키·쿠키로 권한을 주지 않음."""
    from mcp.server.transport_security import TransportSecuritySettings

    return server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                                      host="0.0.0.0")
