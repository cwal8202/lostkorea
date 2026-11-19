"""LostKorea API — 습득물 검색·분실물 신고·알림 엔드포인트(웹 페이지는 site.py).

실행:  uv run uvicorn apps.api.main:app --reload
문서:  http://localhost:8000/docs
"""

from __future__ import annotations

import json
import re
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps import ops, store

from . import search as search_mod
from . import admin, pii, site, thumbs
from .config import settings
from .gemini import normalize_lang
from .limits import client_ip, limiter
from .models import (
    MAX_TEXT,
    AlertSettingsRequest,
    ConfirmRequest,
    DismissRequest,
    FoundItem,
    LostItemImageRequest,
    LostItemRequest,
    MatchResponse,
    MineRequest,
    OutcomeRequest,
    SearchResponse,
    VerifyRequest,
)

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_HOUR, _DAY = 3600, 86400

app = FastAPI(title=f"{settings.brand_name} API", version="1.0.0",
              description="분실물 자연어 질의 → 습득물 하이브리드 검색 + 지역 가점 + LLM grading")
store.init()  # 분실물 테이블 생성(멱등)


def _alert_retry_loop() -> None:
    """실패한 알림 메일을 15분마다 다시 보냄(15분·1시간·6시간·24시간 뒤) — 밤 재매칭까지 하루를 기다리지 않게."""
    import time

    from apps.agent.rematch import retry_pending_alerts

    while True:
        time.sleep(settings.alert_retry_check_minutes * 60)
        try:
            retry_pending_alerts(verbose=True)
        except Exception as e:  # noqa: BLE001 — 재발송 실패가 서버를 멈추지 않게
            print(f"[알림 재발송] 실패: {type(e).__name__}: {e}")


@app.on_event("startup")
def _start_background() -> None:
    if settings.app_env == "production":  # 로컬·테스트에선 돌지 않음
        import threading

        threading.Thread(target=_alert_retry_loop, name="alert-retry", daemon=True).start()
        print(f"[알림 재발송] {settings.alert_retry_check_minutes}분마다 재발송 대기열 확인 시작", flush=True)


_WEB = Path(__file__).resolve().parents[2] / "apps" / "web"
_is_bot = site.is_bot


_REDIRECT_HOSTS = {h.strip().lower() for h in settings.redirect_hosts.split(",") if h.strip()}


@app.middleware("http")
async def _canonical_host(request: Request, call_next):
    """옛 주소로 온 요청 → 대표 주소(PUBLIC_BASE_URL)로 영구 이동. GET은 301, 나머지는 308(메서드·본문 유지).
    서버 안 점검(localhost)·IP 접속은 목록에 없으니 그대로."""
    host = request.headers.get("host", "").split(":")[0].lower()
    if host in _REDIRECT_HOSTS:
        url = settings.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(url, status_code=301 if request.method in ("GET", "HEAD") else 308)
    return await call_next(request)


@app.middleware("http")
async def _report_unhandled(request: Request, call_next):
    """처리 못 한 예외 → 관리자 메일(종류별 1시간 1통) + 오류 통계. 응답은 기존대로 500."""
    try:
        return await call_next(request)
    except Exception as e:
        route = request.scope.get("route")
        await run_in_threadpool(ops.report_error, f"{request.method} {getattr(route, 'path', request.url.path)}", e)
        raise


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 429:  # 요청 제한에 걸린 횟수(남용·한도 조정 판단용)
        await run_in_threadpool(store.bump, "rate_limited")
    return await http_exception_handler(request, exc)

# 정적 자산은 StaticFiles로 마운트(/static/...). 프로덕션에선 nginx/CDN이 대신 서빙.
app.mount("/static", StaticFiles(directory=_WEB / "static"), name="static")
app.include_router(site.router)  # 웹사이트 페이지(홈·전체 습득물·가이드·방침·약관·robots·sitemap·manifest)
app.include_router(admin.router)  # 운영 화면(/admin) — ADMIN_PASSWORD가 있을 때만, 없으면 404


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "index": settings.opensearch_index, "os": settings.opensearch_url}


@app.get("/health/deep", include_in_schema=False)
def health_deep() -> JSONResponse:
    """외부 감시용 — 검색 엔진(OpenSearch)·DB까지 실제로 응답하는지. 하나라도 실패하면 503."""
    checks = {}
    try:
        url = f"{settings.opensearch_url}/{settings.opensearch_index}/_count"
        checks["search"] = json.load(urllib.request.urlopen(url, timeout=5))["count"] > 0
    except Exception:  # noqa: BLE001
        checks["search"] = False
    try:
        checks["db"] = bool(store._one("SELECT 1 AS ok"))
    except Exception:  # noqa: BLE001
        checks["db"] = False
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "fail", **checks}, status_code=200 if ok else 503)


@app.get("/found-items/search", response_model=SearchResponse)
def search_found_items(
    request: Request,
    q: str = Query(..., max_length=MAX_TEXT, description="분실물 자연어 설명 (예: '지하철에 두고 온 흰색 무선이어폰')"),
    region: str | None = Query(None, max_length=300, description="검색 지역집합, 쉼표구분 (예: '순천,광양,여수'). 넓게(구/시)."),
    lost_date: str | None = Query(None, max_length=10, description="분실일 YYYY-MM-DD — 그 전날 이후 습득물만"),
    grade: bool = Query(False, description="LLM grading 재정렬+근거 (Gemini 호출)"),
    size: int = Query(10, ge=1, le=50, description="반환 개수"),
    candidates: int = Query(100, ge=10, le=200, description="KNN 후보 수 (넉넉히)"),
    w_region: float = Query(0.05, ge=0.0, le=1.0, description="지역 가점 가중치 (eval 권장 ~0.05)"),
) -> SearchResponse:
    limiter.hit(f"search:{client_ip(request)}", settings.limit_search_hour, _HOUR)
    limiter.spend(2 if grade else 1)  # 임베딩 1 + (판정 LLM 1)
    store.bump("search_api")
    region_set = [r.strip() for r in region.split(",") if r.strip()] if region else []
    items = search_mod.search(
        q, region_set=region_set, lost_date=lost_date, do_grade=grade,
        size=size, candidates=candidates, w_region=w_region,
    )
    return SearchResponse(
        query=q, count=len(items), region_set=region_set, graded=grade,
        results=[FoundItem(**it) for it in items],
    )


def _valid_email(email: str) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email))


def _send_verification(lid: str, email: str, lang: str) -> None:
    """확인 메일 발송 — 같은 주소로 하루 N통 제한(남의 주소에 확인 메일 폭탄 방지). 고정 문구 + 링크만."""
    from apps.notifier import notifier

    limiter.hit(f"verifymail:{email.lower()}", settings.limit_verify_mail_day, _DAY,
                message="이 이메일로 확인 메일을 너무 많이 보냈어요. 내일 다시 시도해주세요.")
    token = store.new_token()
    expires = (datetime.now(timezone.utc) + timedelta(hours=settings.verify_hours)).isoformat()
    store.set_pending_email(lid, email, store.hash_token(token), expires)
    if not notifier.verify(email, lang, f"{settings.public_base_url}/email/verify?token={token}"):
        store.bump("verify_mail_failed")  # 이메일은 '확인 대기'로 저장됨 — 알림 설정에서 다시 저장하면 다시 보냄
        raise HTTPException(status_code=502, detail="확인 메일을 보내지 못했어요. 잠시 뒤 알림 설정에서 다시 저장해 주세요. "
                                                    "(We couldn't send the confirmation email — please try again shortly.)")


def _persist_and_respond(text: str, result: dict, email: str,
                         notify_weak: bool = False, lang: str = "ko") -> MatchResponse:
    """매칭 결과 저장 + 응답 구성. 텍스트/사진 신고 공용.

    - 신고 관리 토큰 발급(해시만 저장) — 확인·제외·알림 설정에 필요, 응답으로 1회만 전달.
    - 메일에 실을 후보는 서버가 판정한 것만 저장(`matches`) — 클라이언트 데이터로 메일을 만들지 않음.
    - 이메일을 남기면 확인 메일만 보냄(double opt-in). 결과·알림 메일은 확인 후에.
    """
    from apps.agent.graph import graded_keys
    from apps.agent.rematch import alert_level, slim_matches, watch_until_after

    raw_matches = result.get("matches", [])
    top = raw_matches[0] if raw_matches else None
    best_grade = top.get("grade") if top else None
    email = (email or "").strip()
    if not _valid_email(email):
        email = ""
    if email:
        store.bump("email_given")
    notify_weak = bool(notify_weak and email)
    # strong이면 '유력 후보(candidate)' — 확정 아님, 사용자 확인 필요(HITL).
    status = "candidate" if alert_level(best_grade, False, top) == "strong" else "open"

    token = store.new_token()
    watch_until = watch_until_after()  # 기본 기간(결과창 '알림 설정'에서 변경)
    lid = store.add(
        text, result.get("extracted", {}), result.get("region_set", []),
        result.get("queries", []), top, best_grade, status=status, email="",
        notify_weak=notify_weak, watch_until=watch_until, lang=lang,
        token_hash=store.hash_token(token), matches=slim_matches(raw_matches),
        graded_ids=graded_keys(raw_matches),  # 재매칭이 같은 후보를 다시 판정하지 않게
    )
    email_pending = False
    if email:
        try:
            _send_verification(lid, email, lang)
            email_pending = True
        except HTTPException:  # 확인 메일 한도 초과 — 검색 결과는 그대로 돌려줌
            pass
    return MatchResponse(
        id=lid, token=token, email_pending=email_pending, status=status, watch_until=watch_until,
        query=text, extracted=result.get("extracted", {}),
        region_set=result.get("region_set", []),
        queries=result.get("queries", []),
        count=len(raw_matches),
        matches=[FoundItem(**m) for m in raw_matches],
    )


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 프록시 버퍼링 없이 즉시 전달


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _step_payload(part: dict) -> dict:
    """단계 결과 → 화면용 JSON. 후보·매칭은 FoundItem 스키마로 정리(내부 필드 제거)."""
    return {k: ([FoundItem(**m).model_dump() for m in v or []] if k in ("candidates", "matches") else v)
            for k, v in part.items()}


def _stream_match(steps, email: str, notify_weak: bool, text: str | None = None, lang: str = "ko"):
    """단계 스트림 → SSE 이벤트. 끝나면 저장 후 `result`(MatchResponse).

    이벤트: extract · route · fanout · search(판정 전 후보) · grade · [visual] · result | error
    """
    try:
        final = None
        for step, part in steps:
            if step == "done":
                final = part
                break
            yield _sse(step, _step_payload(part))
        if final is None:
            return
        ex = final.get("extracted", {})
        query = text or final.get("text", "사진 신고")
        if not ex.get("is_lost_report", True):  # 분실물 신고 아님 → 저장·검색·알림 없이 종료
            store.bump("report_invalid")
            resp = MatchResponse(id="", status="invalid", query=query, extracted=ex, matches=[])
        else:
            resp = _persist_and_respond(query, final, email, notify_weak, lang)
        yield _sse("result", resp.model_dump())
    except Exception as e:  # noqa: BLE001 — 스트림 도중 실패는 HTTP 상태로 못 알리므로 이벤트로
        traceback.print_exc()
        ops.report_error("신고 스트림", e)
        yield _sse("error", {"detail": "처리 중 오류가 발생했어요. 잠시 후 다시 시도해주세요."})


def _guard_report(request: Request, image: bool = False) -> None:
    """신고 1건 = 추출·임베딩·판정 LLM(사진은 Vision·사진 대조까지) → IP별 한도 + 하루 전체 상한."""
    ip = client_ip(request)
    if image:
        limiter.hit(f"img10:{ip}", settings.limit_image_10min, 600)
        limiter.hit(f"imgday:{ip}", settings.limit_image_day, _DAY)
        limiter.spend(3)
    else:
        limiter.hit(f"rep10:{ip}", settings.limit_report_10min, 600)
        limiter.hit(f"repday:{ip}", settings.limit_report_day, _DAY)
        limiter.spend(1)
    store.bump("report_image" if image else "report_text")


def _scrub(req) -> None:
    """신고 문장·사진 메모의 전화번호·카드번호·주민번호·여권번호·이메일을 저장·AI 전송 전에 가림(apps/api/pii.py)."""
    n = 0
    for field in ("text", "note"):
        if isinstance(getattr(req, field, None), str):
            masked, k = pii.mask(getattr(req, field))
            if k:
                setattr(req, field, masked)
                n += k
    if n:
        store.bump("pii_masked", n)


@app.post("/lost-items/stream")
def register_lost_item_stream(req: LostItemRequest, request: Request) -> StreamingResponse:
    """`POST /lost-items`의 실시간 버전 — 추출·지역·검색·판정을 단계마다 SSE로 흘려보냄."""
    from apps.agent import graph as agent_graph

    _guard_report(request)
    _scrub(req)
    lang = normalize_lang(req.lang)
    steps = agent_graph.stream(req.text, req.lost_date, lang)
    return StreamingResponse(_stream_match(steps, req.email or "", req.notify_weak, text=req.text, lang=lang),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/lost-items/image/stream")
def register_lost_item_image_stream(req: LostItemImageRequest, request: Request) -> StreamingResponse:
    """`POST /lost-items/image`의 실시간 버전 — 사진 분석·지역·검색·판정·사진 대조를 단계마다 SSE로."""
    from apps.agent import graph as agent_graph

    _guard_report(request, image=True)
    _scrub(req)
    lang = normalize_lang(req.lang)
    steps = agent_graph.stream_image(req.image_b64, req.mime, note=req.note,
                                     lost_date=req.lost_date, lang=lang)
    return StreamingResponse(_stream_match(steps, req.email or "", req.notify_weak, lang=lang),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/thumb.jpg", include_in_schema=False)
def thumbnail(request: Request, u: str = Query(..., max_length=500)) -> Response:
    """경찰 습득물 사진의 작은 미리보기(256px, ~20KB) — 경로가 .jpg라 Cloudflare가 주소(쿼리 포함)별로 저장해 대신 응답.

    경찰민원24 습득물 사진 주소만 허용. 횟수 초과·줄이기 실패면 원본으로 넘김(사진이 사라지지 않게).
    """
    if not thumbs.allowed(u):
        raise HTTPException(status_code=400, detail="허용되지 않은 사진 주소")
    try:
        limiter.hit(f"thumb:{client_ip(request)}", settings.limit_thumb_hour, _HOUR)
        data = thumbs.make(u)
    except Exception:  # noqa: BLE001
        return RedirectResponse(u, status_code=302)
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=2592000, immutable"})


@app.get("/found-items/browse")
def browse_found_items(
    request: Request,
    response: Response,
    q: str = Query("", max_length=100, description="키워드 검색(물품명·설명·분류, BM25)"),
    region: str = Query("", max_length=60, description="지역(구/시) 필터 (예: '강남')"),
    date_from: str = Query("", max_length=10, description="습득일 시작 YYYY-MM-DD"),
    date_to: str = Query("", max_length=10, description="습득일 끝 YYYY-MM-DD"),
    page: int = Query(1, ge=1, description="페이지(1부터)"),
    size: int = Query(24, ge=1, le=100, description="페이지당 개수"),
    photo: str = Query("", pattern="^(|with|without)$", description="사진: with=있는 것만, without=없는 것만, 빈값=전체"),
    sort: str = Query("new", pattern="^(new|old)$", description="정렬: new=최신순, old=오래된순(습득일 기준)"),
    expand: bool = Query(False, description="브랜드·모델을 일반 이름으로도 찾기(에어팟 → 무선이어폰) — 친 그대로가 먼저"),
) -> dict:
    """전체 습득물 둘러보기 — 키워드/지역/날짜/사진 필터 + 최신순·오래된순. 임베딩 미사용(외국어 키워드만 번역 1회·캐시)."""
    if page * size > 10_000:  # OpenSearch 기본 max_result_window
        raise HTTPException(status_code=400, detail="더 이상 볼 수 없는 페이지예요. 검색 조건을 좁혀주세요.")
    if search_mod.needs_translation(q):  # 캐시에 없는 외국어 검색어만 LLM 호출 → 한도·예산 소비
        limiter.hit(f"translate:{client_ip(request)}", settings.limit_translate_hour, _HOUR)
        limiter.spend(1)
    if page == 1 and not _is_bot(request):  # 첫 페이지 조회만(페이지 넘김은 같은 조회)
        store.bump("browse")
    # robots.txt가 이 주소를 연 건 구글이 /browse 화면을 그릴 때 목록을 받게 하려는 것 — 데이터 주소 자체는 검색 결과에 안 뜨게
    response.headers["X-Robots-Tag"] = "noindex"
    return search_mod.browse(q=q, region=region, date_from=date_from, date_to=date_to,
                             page=page, size=size, photo=photo, sort=sort, expand=expand)


@app.post("/lost-items", response_model=MatchResponse)
def register_lost_item(req: LostItemRequest, request: Request) -> MatchResponse:
    """분실물 자연어 신고 → 매칭 에이전트(추출→라우팅→fan-out→검색→grading) → 매칭 결과.

    등록 즉시 매칭 + DB 저장(open/candidate). 신규 습득물 유입 시 지속 재매칭.
    """
    from apps.agent import graph as agent_graph

    _guard_report(request)
    _scrub(req)
    lang = normalize_lang(req.lang)
    result = agent_graph.run(req.text, req.lost_date, lang)
    if not result.get("extracted", {}).get("is_lost_report", True):  # 분실물 신고 아님 → 저장·검색·알림 없이 종료
        store.bump("report_invalid")
        return MatchResponse(id="", status="invalid", query=req.text,
                             extracted=result.get("extracted", {}), matches=[])
    return _persist_and_respond(req.text, result, req.email or "", req.notify_weak, lang)


@app.post("/lost-items/image", response_model=MatchResponse)
def register_lost_item_image(req: LostItemImageRequest, request: Request) -> MatchResponse:
    """사진(+선택 메모) 신고 → Vision 추출 → 동일 파이프라인 → 매칭 결과."""
    from apps.agent import graph as agent_graph

    _guard_report(request, image=True)
    _scrub(req)
    lang = normalize_lang(req.lang)
    result = agent_graph.run_image(req.image_b64, req.mime, note=req.note,
                                   lost_date=req.lost_date, lang=lang)
    return _persist_and_respond(result.get("text", "사진 신고"), result, req.email or "",
                                req.notify_weak, lang)


def _public_item(it: dict) -> dict:
    """'내 신고' 목록용 — 화면에 필요한 필드만(이메일 등 개인정보·내부 필드 제외)."""
    bm = it.get("best_match") or None
    return {
        "id": it.get("id"),
        "text": it.get("text", ""),
        "status": it.get("status"),
        "best_match": {"name": bm.get("name")} if bm else None,
        "best_grade": it.get("best_grade"),
        "outcome": (it.get("outcome") or {}).get("stage"),
        "created_at": it.get("created_at"),
        "watch_until": it.get("watch_until"),
        "email_verified": it.get("email_verified", False),
    }


_PRIVATE_FIELDS = ("email", "user_id", "token_hash", "verify_hash", "verify_expires", "graded_ids", "link_hashes")


def _mask_email(email: str) -> str:
    """알림 받는 이메일을 본인이 알아볼 만큼만(m•••@example.com) — 화면·응답에 전체 주소를 싣지 않음."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}•••@{domain}" if local and domain else ""


def _owned(lid: str, token: str | None) -> dict:
    """신고 관리 토큰 확인. 틀리면 존재 여부도 숨기고 404."""
    row = store.get(lid)
    if not store.check_token(row, token):
        raise HTTPException(status_code=404, detail="해당 분실물 신고 없음")
    return row


@app.post("/lost-items/mine")
def my_lost_items(req: MineRequest) -> list[dict]:
    """'내 신고' — 이 브라우저가 가진 (id, 관리 토큰)으로만 조회. 다른 사람 신고는 볼 수 없음."""
    tokens = {m.id: m.token for m in req.items}
    rows = [r for r in store.get_many(list(tokens)) if store.check_token(r, tokens.get(r["id"]))]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [_public_item(r) for r in rows]


@app.get("/lost-items/{lid}")
def get_lost_item(lid: str, x_report_token: str | None = Header(None),
                  via: str = Query("", max_length=10)) -> dict:
    """신고 상세 — 관리 토큰(또는 알림 메일 링크 토큰)을 가진 신고자만. 이메일은 가린 형태로만.

    via=email: 알림 메일의 '결과 보기' 링크로 들어온 것 → 익명 통계(email_link_open) 1 — 메일 업체의 열람·링크 추적 대신
    우리 사이트에서 직접 셈(누가 열었는지는 남기지 않고 횟수만).
    """
    item = _owned(lid, x_report_token)
    if via == "email":
        store.bump("email_link_open")
    out = {k: v for k, v in item.items() if k not in _PRIVATE_FIELDS}
    out["email_masked"] = _mask_email(item.get("email") or "")
    return out


@app.post("/lost-items/{lid}/alerts")
def update_lost_item_alerts(lid: str, req: AlertSettingsRequest, request: Request,
                            x_report_token: str | None = Header(None)) -> dict:
    """결과창 '알림 설정' 저장 — 이메일·약한 후보 옵트인·알림 기간.

    - 알림 기간: 지금부터 watch_days일 동안 새 습득물과 매일 재매칭. 만료(expired) 신고는 저장 시 재개.
    - 새 이메일이면 확인 메일만 발송 — 링크를 눌러야 알림·현재 결과 메일이 감(남의 주소로 못 보냄).
    """
    from apps.agent.rematch import watch_until_after

    limiter.hit(f"alerts:{client_ip(request)}", settings.limit_alerts_hour, _HOUR)
    item = _owned(lid, x_report_token)
    # email 생략(None) = 지금 이메일 유지 — 기간만 바꾸려다 빈 칸 때문에 알림이 꺼지지 않게. ""는 알림 끄기.
    email = (item.get("email") or "") if req.email is None else req.email.strip()
    if email and not _valid_email(email):
        raise HTTPException(status_code=400, detail="유효한 이메일이 아닙니다")
    notify_weak = bool(req.notify_weak and email)
    watch_until = watch_until_after(req.watch_days)
    status = "open" if item.get("status") == "expired" else None  # 만료 신고 → 다시 찾는중
    store.update_alerts(lid, notify_weak, watch_until, status=status)

    verification_sent = False
    if email != (item.get("email") or ""):
        if email:
            _send_verification(lid, email, item.get("lang") or "ko")
            verification_sent = True
        else:
            store.set_pending_email(lid, "", None, None)  # 이메일 비움 → 알림 해제
    verified = bool(email) and email == (item.get("email") or "") and item.get("email_verified", False)
    shown = email if req.email is not None else _mask_email(email)  # 방금 입력한 주소만 그대로, 유지한 주소는 가림
    return {"ok": True, "email": shown, "email_verified": verified, "verification_sent": verification_sent,
            "notify_weak": notify_weak, "watch_until": watch_until,
            "status": status or item.get("status")}


def _mail_page(title: str, body: str, script: str = "") -> str:
    """메일 링크로 여는 작은 화면(한·영 함께 — 메일 링크라 화면 언어를 알 수 없음). 이메일 확인·알림 그만 받기."""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{settings.brand_name} — {title}</title>
<meta name="robots" content="noindex"><link rel="icon" type="image/svg+xml" href="/static/logo.svg">
<link rel="stylesheet" href="/static/fonts/pretendard/pretendard.css?v={site.VERSION}">
<link rel="stylesheet" href="/static/css/site.css?v={site.VERSION}">
<style>body{{background:var(--bg-soft)}}.c{{max-width:480px;margin:56px auto;text-align:center}}
.c .brand{{justify-content:center;margin-bottom:18px}}.c p{{color:#475569;margin:10px 0}}</style></head><body>
<div class="container"><div class="card c">
<a class="brand" href="/"><img src="/static/logo.svg" alt="" width="34" height="34"><span class="wordmark">Lost<span>Korea</span></span></a>
{body}<p class="small"><a href="/">{settings.brand_name} 홈 · Home</a></p></div></div>{script}</body></html>"""


# 확인 링크를 열었을 때 상태별 글 — 이미 확인·만료·없는 링크는 버튼 없이 바로(예전엔 늘 버튼, 눌러야 '만료'가 떴음)
def _verify_done(sent: bool) -> str:
    return ("<b>확인됐어요!</b> 이제 새 습득물이 들어오면 알려드려요." + (" 지금 결과도 메일로 보냈어요." if sent else "")
            + "<br><b>Confirmed</b> — alerts are on." + (" We've emailed you the current results too." if sent else ""))
_VERIFY_MSG = {
    "verified": ("<b>이미 확인된 이메일이에요.</b> 알림이 켜져 있어서 새 습득물이 들어오면 알려드려요.<br>"
                 "<b>Already confirmed.</b> Alerts are on — we'll email you when a match comes in."),
    "expired": (f"<b>확인 링크가 만료됐어요</b>({settings.verify_hours}시간). 결과 화면의 '알림 설정'에서 이메일을 다시 저장하면 "
                f"새 확인 메일을 보내드려요.<br><b>This link has expired</b> ({settings.verify_hours} hours). "
                "Save your email again under Alert settings to get a new one."),
    "invalid": ("<b>더 이상 쓸 수 없는 링크예요.</b> 이메일을 바꿨거나 알림을 껐다면 예전 링크는 쓸 수 없어요. "
                "결과 화면의 '알림 설정'에서 다시 요청해주세요.<br><b>This link is no longer valid.</b> "
                "If you changed your email or turned alerts off, request a new one under Alert settings."),
}
_VERIFY_JS = """<script>
async function go(){const b=document.getElementById("b"),m=document.getElementById("m");b.disabled=true;
m.textContent="확인 중… · Confirming…";const t=new URLSearchParams(location.search).get("token")||"";
try{const r=await fetch("/email/verify",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({token:t})});const d=await r.json();
b.remove();m.innerHTML=r.ok?(d.message||""):(typeof d.detail==="string"?d.detail:"오류 · Error")}
catch(e){m.textContent="네트워크 오류 · Network error";b.disabled=false}}</script>"""


def _unsub_page(body: str) -> str:
    """'알림 그만 받기' 화면."""
    return _mail_page("알림 그만 받기 / Stop alerts", body)


@app.get("/unsubscribe", include_in_schema=False)
def unsubscribe_page(r: str = Query("", max_length=200)) -> HTMLResponse:
    """알림 메일의 '알림 그만 받기' → 확인 버튼 화면. GET은 아무것도 바꾸지 않음(메일 보안 스캐너가 링크를 미리 열어도 안 꺼지게)."""
    import html as _h

    body = (f'<p>이 분실물 신고의 이메일 알림을 끌까요?<br>Turn off email alerts for this lost-item report?</p>'
            f'<form method="post" action="/unsubscribe?r={_h.escape(r, quote=True)}">'
            f'<button class="btn btn-primary btn-lg" type="submit">알림 그만 받기 · Stop alerts</button></form>')
    return HTMLResponse(_unsub_page(body), headers={"Cache-Control": "no-store"})


@app.post("/unsubscribe", include_in_schema=False)
def unsubscribe(request: Request, r: str = Query("", max_length=200)) -> HTMLResponse:
    """수신 거부 — 확인 화면의 버튼 + 메일 앱의 원클릭(RFC 8058, 'List-Unsubscribe=One-Click' POST).

    그 신고의 알림 이메일을 지움(신고·결과는 남음 — 다시 받으려면 결과 화면에서 이메일 입력).
    토큰이 틀려도 같은 화면(어떤 신고가 있는지 알려 주지 않음). 메일 업체 서버가 대신 보낼 수 있어 IP 한도는 넉넉히.
    """
    limiter.hit(f"unsub:{client_ip(request)}", 120, _HOUR)
    lid, _, token = r.partition(".")
    row = store.get(lid) if lid else None
    if store.check_unsub(row, token) and row.get("email"):
        store.set_pending_email(lid, "", None, None)
        store.bump("unsubscribe")
    body = ("<p><b>이메일 알림을 껐어요.</b> 다시 받으려면 결과 화면의 '알림 설정'에서 이메일을 입력하세요.<br>"
            "<b>Email alerts are off.</b> To turn them back on, enter your email under Alert settings.</p>")
    return HTMLResponse(_unsub_page(body), headers={"Cache-Control": "no-store"})


@app.get("/email/verify", include_in_schema=False)
def verify_email_page(request: Request, token: str = Query("", max_length=200)) -> HTMLResponse:
    """확인 메일 링크 → 확인할 수 있으면 버튼, 이미 확인·만료·없는 링크면 그 안내를 바로.

    GET은 상태를 읽기만 함 — 메일 보안 스캐너가 링크를 미리 열어도 저절로 확인되지 않게 확인은 버튼(POST)으로.
    """
    limiter.hit(f"verifyview:{client_ip(request)}", 120, _HOUR)
    state, _ = store.verify_state(token)
    if state != "ok":
        return HTMLResponse(_mail_page("이메일 확인 / Confirm email", f"<p>{_VERIFY_MSG[state]}</p>"),
                            headers={"Cache-Control": "no-store"})
    body = ("<p>분실물 알림을 받으려면 아래 버튼을 눌러 이메일을 확인해주세요.<br>"
            "Press the button below to confirm your email and turn on lost-item alerts.</p>"
            '<button id="b" class="btn btn-primary btn-lg" onclick="go()">알림 받기 확인 · Confirm</button>'
            '<p id="m" aria-live="polite"></p>')
    return HTMLResponse(_mail_page("이메일 확인 / Confirm email", body, _VERIFY_JS), headers={"Cache-Control": "no-store"})


@app.post("/email/verify")
def verify_email(req: VerifyRequest, request: Request) -> dict:
    """이메일 확인 → 알림 활성화. 저장된 후보(서버 판정)가 알림 기준을 넘으면 현재 결과도 바로 발송."""
    from apps.agent.rematch import alert_level

    limiter.hit(f"verify:{client_ip(request)}", settings.limit_alerts_hour, _HOUR)
    row = store.verify_email(req.token)
    if not row:
        state, _ = store.verify_state(req.token)
        if state == "verified":  # 두 번 누름·다른 탭 — 오류가 아니라 '이미 확인'(결과 메일은 다시 안 보냄)
            return {"ok": True, "already": True, "sent_results": False, "message": _VERIFY_MSG["verified"]}
        raise HTTPException(status_code=400, detail=_VERIFY_MSG.get(state, _VERIFY_MSG["invalid"]))
    store.bump("email_verified")
    matches = row.get("matches") or []
    top = matches[0] if matches else None
    level = alert_level(top.get("grade"), row.get("notify_weak", False), top) if top else None
    sent = False
    if level and row.get("status") in ("open", "candidate"):
        from apps.agent.rematch import send_alert
        sent = send_alert(row, matches, weak=(level == "weak"))  # 실패하면 재발송 대기 — 응답은 그대로
    return {"ok": True, "sent_results": sent, "message": _verify_done(sent)}


@app.post("/lost-items/{lid}/confirm")
def confirm_lost_item(lid: str, req: ConfirmRequest, x_report_token: str | None = Header(None)) -> dict:
    """'내 물건이에요' → confirmed. 후보는 서버가 저장한 것에서 찾음(클라이언트 데이터 불신). 수령은 본인(HITL)."""
    item = _owned(lid, x_report_token)
    cands = (item.get("matches") or []) + ([item["best_match"]] if item.get("best_match") else [])
    # 같은 관리번호에 순번만 다른 습득물(가방과 그 안의 지갑 등, 운영 3%)을 헷갈리지 않게 순번까지 맞춤
    match = next((m for m in cands if m.get("atc_id") == req.atc_id
                  and (not req.fd_sn or str(m.get("fd_sn") or "1") == req.fd_sn)), None)
    if not match:
        raise HTTPException(status_code=400, detail="이 신고의 후보가 아니에요")
    store.confirm(lid, match)
    store.bump("confirm")
    return {"ok": True, "status": "confirmed"}


@app.post("/lost-items/{lid}/outcome")
def lost_item_outcome(lid: str, req: OutcomeRequest, x_report_token: str | None = Header(None)) -> dict:
    """수령 진행 — '내 물건이에요' 뒤에 신고자가 누름: 연락했어요·방문했어요·찾았어요·내 물건이 아니었어요.

    '찾았어요'가 실제 회수 기록(서비스가 확인한 게 아니라 신고자가 알려 준 것). '내 물건이 아니었어요'는 그 후보를 빼고 다시 찾음.
    """
    item = _owned(lid, x_report_token)
    if item.get("status") != "confirmed" or not item.get("best_match"):
        raise HTTPException(status_code=400, detail="'내 물건이에요'를 누른 신고에서만 알릴 수 있어요")
    match = item["best_match"]
    outcome, first = store.record_outcome(item, req.stage, match)
    if first:  # 같은 버튼을 여러 번 눌러도 통계는 한 번
        store.bump(f"outcome_{req.stage}")
    status = "confirmed"
    if req.stage == "not_mine":  # 기관에 확인해 보니 다른 물건 → 그 후보 빼고 다시 찾는 중
        store.dismiss(lid, dismiss_key(match, match.get("atc_id"), str(match.get("fd_sn") or "1")))
        status = "open"
    return {"ok": True, "stage": req.stage, "status": status, "outcome": outcome}


def dismiss_key(match: dict | None, atc_id: str, fd_sn: str = "") -> str:
    """'아니에요'로 뺄 범위 — 출처별로 다름(운영 데이터 확인, 11/15).

    경찰서: 같은 관리번호 = 한 사람 소지품 묶음(지갑 + 그 안의 현금·신분증) → 번호 전체를 뺌.
    기관(예: 인천공항세관): 같은 날 들어온 서로 다른 사람 물건을 한 번호로 등록(립스틱 + 담배 쇼핑백) → 누른 그 물건만.
    출처를 모르면 그 물건만(내 물건이 잘못 사라지는 쪽이 더 나쁨). 순번을 모르는 옛 화면 요청은 번호 전체(예전 동작).
    """
    if (match or {}).get("source") == "police" or not fd_sn:
        return atc_id
    return f"{atc_id}_{fd_sn}"


@app.post("/lost-items/{lid}/dismiss")
def dismiss_lost_item(lid: str, req: DismissRequest, x_report_token: str | None = Header(None)) -> dict:
    """'아니에요' → 그 후보 제외하고 계속 탐색(open). 경찰서 습득물은 같은 관리번호 묶음 전체, 기관 습득물은 그 물건만."""
    item = _owned(lid, x_report_token)
    cands = (item.get("matches") or []) + ([item["best_match"]] if item.get("best_match") else [])
    match = next((m for m in cands if m.get("atc_id") == req.atc_id
                  and (not req.fd_sn or str(m.get("fd_sn") or "1") == req.fd_sn)), None)
    dismissed = store.dismiss(lid, dismiss_key(match, req.atc_id, req.fd_sn))
    store.bump("dismiss")
    return {"ok": True, "status": "open", "dismissed": dismissed}


@app.delete("/lost-items/{lid}")
def delete_lost_item(lid: str, x_report_token: str | None = Header(None)) -> dict:
    """신고 삭제(신고자 요청) — 신고 문장·이메일·후보 기록을 즉시 완전 삭제. 이후 알림도 끝."""
    _owned(lid, x_report_token)
    store.delete(lid)
    store.bump("delete")
    return {"ok": True, "deleted": lid}
