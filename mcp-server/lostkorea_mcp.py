# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.3.0", "httpx>=0.27"]
# ///
"""LostKorea MCP 서버(FIND-128) — AI(Claude 등)가 한국 경찰 습득물 AI 매칭을 도구로 쓰게.

실행(이 PC의 Claude Code에 붙이기 예):
  claude mcp add lostkorea -e LOSTKOREA_MCP_KEY=<키> -- uv run --script <저장소>/mcp-server/lostkorea_mcp.py
환경변수: LOSTKOREA_MCP_KEY(운영 서버 .env의 MCP_API_KEYS 중 하나 — find_matches·get_found_item에 필요),
          LOSTKOREA_API_URL(기본 https://lostkorea.com).
도구 3개 — find_matches(신고 1~20건 → AI 매칭, 저장 안 함), search_found_items(키워드·지역·날짜로 찾기, AI 없음),
get_found_item(습득물 한 건 + 경찰 쪽 지금 상태). 서버가 신고 문장의 전화번호 등을 가린 뒤 AI로 보냄.
"""

from __future__ import annotations

import os
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

API = os.environ.get("LOSTKOREA_API_URL", "https://lostkorea.com").rstrip("/")
KEY = os.environ.get("LOSTKOREA_MCP_KEY", "")
HEADERS = {"User-Agent": "LostKorea-MCP/1.0", "X-MCP-Key": KEY}
CHUNK = 5   # 서버 한 요청에 보내는 신고 수(신고 1건 ~9초, 5건을 동시에 → 요청 하나 ~15초)

server = MCPServer(
    name="LostKorea",
    instructions=(
        "LostKorea matches lost-item reports against items handed in to police stations and lost-and-found centers "
        "across Korea (Korean National Police Agency open data, items found within about the last 6 months). "
        "Use find_matches to run reports through the AI matcher (costs money per report), search_found_items to "
        "browse by keyword/region/date (no AI), and get_found_item to check whether an item is still held. "
        "Matches are candidates, not confirmations: the owner must confirm with the holding agency by phone or visit."
    ),
)


class LostReport(BaseModel):
    text: str = Field(description="What was lost, where and when, in any language "
                                  "(e.g. 'Black leather wallet left on subway line 2 near Hongdae, Nov 18'). 2-500 chars.",
                      min_length=2, max_length=500)
    lost_date: str | None = Field(None, description="Date lost, YYYY-MM-DD, if known (only items found from the day "
                                                    "before are considered)")
    lang: str | None = Field(None, description="Language for AI reasons and translated names, e.g. 'en', 'ko', 'ja' "
                                               "(default 'ko')")


async def _post(path: str, body: dict) -> dict:
    if not KEY:
        raise RuntimeError("LOSTKOREA_MCP_KEY is not set — ask the LostKorea operator for an MCP key.")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(API + path, json=body, headers=HEADERS)
    return _ok(r)


async def _get(path: str, params: dict, key: bool = False) -> dict:
    if key and not KEY:
        raise RuntimeError("LOSTKOREA_MCP_KEY is not set — ask the LostKorea operator for an MCP key.")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(API + path, params=params, headers=HEADERS if key else {"User-Agent": HEADERS["User-Agent"]})
    return _ok(r)


def _ok(r: httpx.Response) -> dict:
    if r.status_code == 200:
        return r.json()
    try:
        detail = r.json().get("detail")
    except Exception:  # noqa: BLE001
        detail = r.text[:200]
    hint = {401: "MCP key rejected", 404: "MCP access is not enabled on this server", 429: "rate limit reached"}
    raise RuntimeError(f"LostKorea API {r.status_code} ({hint.get(r.status_code, 'error')}): {detail}")


@server.tool(
    title="Find matching found items for lost-item reports",
    annotations=ToolAnnotations(title="Find matches", read_only_hint=True, open_world_hint=True),
)
async def find_matches(reports: list[LostReport], top: int | None = None) -> dict:
    """Run 1-20 lost-item reports through LostKorea's AI matcher (the same one as the website) and return candidates.

    For each report: what the AI extracted (item, color, brand, place, date), the regions searched (routes such as
    subway or bus lines are followed into other districts), a summary ('strong' = a candidate scored 80+,
    'weak' = 50-79, 'none'), and up to `top` candidates (default 8 for one report, 3 each for several; max 8).
    Each candidate has a 0-100 score, the AI's reason, whether place/date line up, the item details (Korean original
    plus English), holding agency and phone, photo, the police listing URL and map links.
    Nothing is saved and no alerts are sent. Each report costs roughly USD 0.004 of AI and counts toward the
    key's daily limit (300 reports). Phone/card numbers in the text are masked before AI.
    Treat results as candidates: the owner must confirm with the agency (phone or visit) before claiming.
    """
    if not 1 <= len(reports) <= 20:
        raise ValueError("Send 1-20 reports per call.")
    results: list[dict] = []
    for i in range(0, len(reports), CHUNK):
        part = [r.model_dump(exclude_none=True) for r in reports[i : i + CHUNK]]
        body: dict = {"reports": part}
        if top:
            body["top"] = max(1, min(8, top))
        elif len(reports) == 1:
            body["top"] = 8
        else:
            body["top"] = 3
        results += (await _post("/mcp-api/match", body))["results"]
    return {"results": results}


def _slim(m: dict) -> dict:
    """전체 습득물 목록 한 건 → 도구 응답(웹 카드와 같은 정보, 한국어 + 영어)."""
    url = m.get("image_url") or ""
    return {
        "atc_id": m.get("atc_id"), "fd_sn": m.get("fd_sn") or "1",
        "name": m.get("name"), "name_en": m.get("name_mt") or "",
        "category": m.get("category"), "category_en": m.get("category_en", ""),
        "color": m.get("color"), "color_en": m.get("color_en", ""),
        "found_at": m.get("found_at"), "region": m.get("region"), "region_en": m.get("region_en", ""),
        "found_place_type": m.get("found_place"), "found_place_type_en": m.get("found_place_en", ""),
        "agency": m.get("org_name"), "agency_en": m.get("org_mt", ""), "phone": m.get("tel"),
        "photo_url": url if url.startswith("http") and "no_img" not in url else "",
        "police_url": ("https://minwon24.police.go.kr/cvlcpt/selectFindListDetail.do?cvlcptId=MW-201"
                       f"&pkupCmdtyMngId={m.get('atc_id')}&sortSn={m.get('fd_sn') or '1'}"),
    }


@server.tool(
    title="Browse found items by keyword, region and date",
    annotations=ToolAnnotations(title="Search found items", read_only_hint=True, open_world_hint=True),
)
async def search_found_items(keyword: str = "", region: str = "", date_from: str = "", date_to: str = "",
                             photo_only: bool = False, sort: Literal["new", "old"] = "new", page: int = 1,
                             size: int = 10, include_similar_names: bool = False) -> dict:
    """Browse found items like the website's 'All found items' page — plain keyword search, no AI cost.

    keyword: item words in Korean or any language (non-Korean is translated to Korean once). Spelling variants are
    matched automatically (보조밧데리 = 보조배터리). include_similar_names=True also returns items the police listed
    under a generic name (에어팟 → 무선이어폰), exact matches first.
    region: Korean province or 'province district' exactly as the site uses (e.g. '서울특별시' or '서울특별시 강남구').
    date_from/date_to: found date YYYY-MM-DD. size: 1-30.
    """
    params = {"q": keyword, "region": region, "date_from": date_from, "date_to": date_to, "page": max(1, page),
              "size": max(1, min(30, size)), "photo": "with" if photo_only else "", "sort": sort}
    if include_similar_names:
        params["expand"] = "1"
    d = await _get("/found-items/browse", params)
    out = {"total": d.get("total"), "page": d.get("page"), "items": [_slim(m) for m in d.get("items") or []]}
    x = d.get("expand")
    if x and not x.get("on"):
        out["similar_names_available"] = {"generic_names": x.get("words"), "extra_items": x.get("extra"),
                                          "hint": "call again with include_similar_names=true"}
    elif x and x.get("on"):
        out["similar_names_included"] = {"generic_names": x.get("words"), "exact_matches_first": x.get("exact"),
                                         "auto": bool(x.get("auto"))}
    return out


@server.tool(
    title="Check one found item and its current police status",
    annotations=ToolAnnotations(title="Get found item", read_only_hint=True, open_world_hint=True),
)
async def get_found_item(atc_id: str, fd_sn: str = "1") -> dict:
    """Look up one found item by its police ID (atc_id, e.g. 'F2026111800003998'; fd_sn usually '1').

    Returns our copy of the item (if still in the LostKorea index) and the police system's current status
    (e.g. '보관중' = still held, '종결' = closed: returned to the owner, transferred or disposed).
    Items leave our index when closed or after about 6 months, so in_our_index=false with a police status is normal.
    """
    return await _get(f"/mcp-api/items/{atc_id}", {"fd_sn": fd_sn}, key=True)


if __name__ == "__main__":
    server.run(transport="stdio")
