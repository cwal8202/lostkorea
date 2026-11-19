"""운영 화면(/admin) — 운영자만 보는 숫자판. 읽는 순서: 확인할 것(이상만 위로) → 기간별 숫자(신고 대장이 중심) → 지금 상태.

- 보호: .env `ADMIN_PASSWORD`가 있을 때만 열림(HTTP Basic, 사용자 이름은 아무거나, 상수 시간 비교).
  값이 없으면 /admin 아래 전부 404(없는 주소와 같은 응답). 같은 IP가 한 시간에 limit_admin_fail_hour번 틀리면
  그 IP는 한 시간 동안 맞는 비밀번호도 429. 응답은 저장·색인 금지(no-store·noindex), robots.txt도 Disallow.
- 숫자는 실제 기록에서만: metrics(일·종류별 횟수)·lost_items(신고)·found_closures(습득물 이탈)·OpenSearch(색인)·
  이 프로세스 상태(예비 AI 모델·오늘 AI 사용량). 기록하지 않는 값은 지어내지 않고 '＊ 기록 없음', 다른 기록으로 어림한 값은 '≈'.
- 개인정보: 목록(/admin/data)엔 신고 문장·이메일이 없다. 줄을 열 때(/admin/reports/{id})만 신고 문장을 보내고 연 시각을 남긴다.
  이메일 주소는 어디에도 싣지 않는다(확인 여부만). 외국어 신고는 처음 열 때 AI가 한국어로 옮겨 신고에 저장(신고와 함께 지워짐).
- JSON 주소는 `X-Admin: 1` 머리글이 있어야 응답 — 다른 사이트가 운영자 브라우저의 로그인으로 몰래 부르지 못하게(CSRF).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from apps import ops, store

from .config import settings
from .limits import client_ip, limiter

router = APIRouter(include_in_schema=False)
KST = timezone(timedelta(hours=9))
_HOUR = 3600
_PAGE = Path(__file__).resolve().parents[2] / "apps" / "web" / "admin.html"
_REALM = 'Basic realm="LostKorea admin", charset="UTF-8"'
_NO_STORE = {"Cache-Control": "no-store, private", "X-Robots-Tag": "noindex, nofollow",
             "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
SPAN_MAX = 90            # 화면 기간 버튼 최대(7·30·90일) — 앞 기간 비교까지 2배를 읽음
CATCHUP_CAP = 600_000    # 수집기 --catch-up-max-docs 기본값(apps/collector/run.py) — 늦은 등록 채우기를 멈추는 색인 크기
MAIL_FREE_MONTH = 3000   # Maileroo 무료 한 달 발송 한도
DISK_WARN, DISK_CRIT = 85, 95


# ── 보호 ──
def _basic_password(header: str) -> str | None:
    """'Basic base64(user:pass)' → pass. 형식이 아니면 None."""
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        raw = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return raw.partition(":")[2] if ":" in raw else None


def require_admin(request: Request) -> None:
    pw = settings.admin_password
    if not pw:
        raise HTTPException(status_code=404, detail="Not Found")
    key = f"adminfail:{client_ip(request)}"
    if limiter.count(key, _HOUR) >= settings.limit_admin_fail_hour:
        raise HTTPException(status_code=429, detail="로그인 실패가 너무 많아요. 한 시간 뒤 다시 시도하세요.",
                            headers={"Retry-After": str(_HOUR), **_NO_STORE})
    given = _basic_password(request.headers.get("authorization", ""))
    # 길이가 달라도 같은 시간이 걸리게 해시끼리 비교
    ok = given is not None and hmac.compare_digest(hashlib.sha256(given.encode()).digest(),
                                                   hashlib.sha256(pw.encode()).digest())
    if not ok:
        if given is not None:  # 머리글 없는 첫 요청(브라우저가 로그인 창을 띄우기 전)은 실패로 세지 않음
            limiter.hit(key, settings.limit_admin_fail_hour, _HOUR)
            store.bump("admin_auth_fail")
        raise HTTPException(status_code=401, detail="로그인이 필요해요.",
                            headers={"WWW-Authenticate": _REALM, **_NO_STORE})


def _require_xhr(request: Request) -> None:
    if request.headers.get("x-admin") != "1":
        raise HTTPException(status_code=400, detail="운영 화면에서만 부를 수 있어요.")


# ── 도우미 ──
def _kst(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso or "")
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(KST)


def _kdt(d: datetime | None) -> str:
    return f"{d.month}월 {d.day}일 {d:%H:%M}" if d else ""


def _kday(day: str | date | None) -> str:
    try:
        d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day or "")
    return f"{d.month}월 {d.day}일"


def _os(path: str, body: dict | None = None, timeout: float = 5) -> dict:
    url = f"{settings.opensearch_url}/{settings.opensearch_index}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── 신고 언어(문자 체계로만 — 비용 0) ──
_MASK = re.compile(r"\[[^\]]{1,10}\]")  # [전화번호] 같은 가림 표시는 언어 판단에서 뺌
_SCRIPTS = {
    "ko": re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]"), "ja": re.compile(r"[぀-ヿ]"), "zh": re.compile(r"[一-鿿]"),
    "th": re.compile(r"[฀-๿]"), "ru": re.compile(r"[Ѐ-ӿ]"), "hi": re.compile(r"[ऀ-ॿ]"),
    "latin": re.compile(r"[A-Za-zÀ-ɏ]"),
}
_VI = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE)
LANG_LABEL = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "vi": "베트남어", "th": "태국어",
              "ru": "러시아어", "hi": "힌디어", "fr": "프랑스어", "es": "스페인어", "de": "독일어",
              "id": "인도네시아어", "ms": "말레이어"}
_LATIN_LANGS = {"en", "fr", "es", "de", "id", "ms"}


def text_lang(text: str, ui_lang: str = "ko") -> str:
    """신고 문장의 언어. 한글이 든 낱말이 다른 문자 낱말보다 적지 않으면 한국어 — 글자 수로 세면 'Louis Vuitton 지갑'처럼
    영문 브랜드가 긴 한국어 신고가 영어로, 낱말로 세면 'I lost my phone at 홍대입구역'은 영어로 맞게 나옴.
    그 밖엔 글자 수가 가장 많은 문자 체계. 라틴 문자는 베트남어 부호가 있으면 베트남어, 화면 언어가 라틴 문자 언어면 그 언어, 아니면 영어."""
    t = _MASK.sub(" ", text or "")
    words = t.split()
    ko_w = sum(1 for w in words if _SCRIPTS["ko"].search(w))
    other_w = sum(1 for w in words if not _SCRIPTS["ko"].search(w)
                  and any(rx.search(w) for k, rx in _SCRIPTS.items() if k != "ko"))
    base = (ui_lang or "ko").split("-")[0]
    if ko_w and ko_w >= other_w:
        return "ko"
    n = {k: len(rx.findall(t)) for k, rx in _SCRIPTS.items() if k != "ko"}
    if not sum(n.values()):
        return base if base in LANG_LABEL else "ko"
    if n["ja"]:
        return "ja"
    top = max(n, key=n.get)
    if top == "latin":
        if _VI.search(t):
            return "vi"
        return base if base in _LATIN_LANGS else "en"
    return top


def _synth_core(ex: dict) -> str:
    """사진 신고의 저장 문장 앞부분 — apps/agent/graph.py `_synth_text`와 같은 규칙(색 브랜드 물건 / 특징 / 메모)."""
    core = " ".join(x for x in (ex.get("color", ""), ex.get("brand", ""), ex.get("item", "")) if x)
    feats = ex.get("features", "")
    return " / ".join(p for p in (core, feats) if p) or (ex.get("category", "") or "사진 신고")


def is_image_report(row: dict) -> bool:
    """사진 신고인가 — 신고에 따로 적지 않으므로 저장 문장이 추출 항목으로 만든 문장인지로 판단."""
    text, ex = (row.get("text") or "").strip(), row.get("extracted") or {}
    if not text or not ex:
        return False
    s = _synth_core(ex)
    return text == s or text.startswith(s + " / ")


# ── 신고 대장 한 줄 ──
STAGES = ("신고", "유력 후보", "내 물건", "연락·방문", "찾음")


def _history(row: dict) -> set[str]:
    o = row.get("outcome") or {}
    return {h.get("stage") for h in (o.get("history") or []) if h.get("stage")} | ({o["stage"]} if o.get("stage") else set())


def _grades(row: dict) -> list[int]:
    gs = [m.get("grade") for m in (row.get("matches") or []) if isinstance(m, dict)]
    gs.append(row.get("best_grade"))
    return [g for g in gs if isinstance(g, int)]


def stages(row: dict) -> list[bool]:
    """다섯 단계를 지났는가. '연락·방문'·'찾음'은 신고자가 결과 화면에서 직접 누른 것만(outcome)."""
    hist, status = _history(row), row.get("status")
    strong = status == "candidate" or any(g >= settings.rematch_grade_threshold for g in _grades(row))
    mine = status == "confirmed" or bool(hist)
    return [True, strong, mine, bool(hist & {"contacted", "visited"}), "found" in hist]


def now_label(row: dict) -> str:
    hist, status = _history(row), row.get("status")
    if "found" in hist:
        return "찾았어요"
    if status == "confirmed":
        return "방문했어요" if "visited" in hist else "연락했어요" if "contacted" in hist else "'내 물건이에요'"
    if "not_mine" in hist:
        return "내 물건이 아니었어요"
    return {"expired": "기간 끝(만료)", "candidate": "유력 후보 있음"}.get(status, "찾는 중")


def ledger_row(row: dict) -> dict:
    created = _kst(row.get("created_at"))
    lang = text_lang(row.get("text") or "", row.get("lang") or "ko")
    ex = row.get("extracted") or {}
    return {
        "rid": row["id"], "day": created.date().isoformat() if created else "",
        "at": _kdt(created), "lang": lang, "lang_label": LANG_LABEL.get(lang, lang),
        "cat": (ex.get("category") or ex.get("item") or "기타").strip()[:20],
        "image": is_image_report(row), "alert": bool(row.get("email_verified")),
        "stages": stages(row), "now": now_label(row), "found": "found" in _history(row),
    }


# ── 확인할 것(이상만 위로, 정상은 접어 둠) ──
def _check(name: str, ok: str, level: str | None = None, title: str = "", detail: str = "") -> dict:
    return {"name": name, "ok": ok, "level": level, "title": title, "detail": detail}


def build_checks(now: datetime, m: dict[str, dict[str, int]], idx: dict | None, idx_err: str,
                 new_today: int | None, disk: int | None) -> list[dict]:
    today = now.date()
    yday = (today - timedelta(days=1)).isoformat()
    recent = lambda k: (m.get(today.isoformat(), {}).get(k, 0) + m.get(yday, {}).get(k, 0))  # noqa: E731 — 최근 이틀
    out = []

    # 사이트 응답(검색 엔진·DB)
    if idx is None:
        out.append(_check("사이트 응답", "", "crit", "검색 엔진이 응답하지 않아요",
                          "신고 검색과 오늘 밤 수집이 멈춰요. 서버에서 docker ps로 lostkorea-opensearch가 떠 있는지 보세요. "
                          f"최근 습득일과 색인 수도 지금은 읽지 못해요. ({idx_err})"))
    else:
        out.append(_check("사이트 응답", "검색과 DB 모두 응답해요."))

    # 새벽 수집 — 끝난 시각은 서버 로그에만 있어 여기선 '처음 색인한 날'(indexed_at)로 셈
    if new_today is not None:
        if new_today:
            out.append(_check("새벽 수집＊", f"오늘 새로 색인한 습득물 {new_today:,}건. 끝난 시각은 기록하지 않아요."))
        elif now.hour < 2:
            out.append(_check("새벽 수집＊", "00시에 시작해 아직 새로 색인한 습득물이 없어요(보통 1~7시간)."))
        else:
            out.append(_check("새벽 수집＊", "", "warn", "오늘 새로 색인한 습득물이 없어요",
                              "00시 수집이 실패했거나 아직 색인 전이에요. 실패했다면 '일일 수집 실패' 메일이 갔을 거예요. "
                              "로그: /root/lostkorea/daily_collect.log"))

    # 최근 습득일(수집이 며칠째 멈췄는지)
    if idx is not None:
        latest = (idx.get("latest_found_at") or "")[:10]
        try:
            age = (today - date.fromisoformat(latest)).days
        except ValueError:
            age = None
        if age is not None and age <= 3:
            out.append(_check("최근 습득일", f"{_kday(latest)}({age}일 전). 3일 넘게 멈추면 경보 메일."))
        else:
            out.append(_check("최근 습득일", "", "warn", "새 습득물이 며칠째 들어오지 않아요",
                              f"가장 최근 습득일이 {_kday(latest) or '없음'}"
                              f"{f'({age}일 전)' if age is not None else ''}이에요. 공공데이터 API·수집 cron을 확인하세요."))

    # DB 백업(새벽 4시 cron이 metrics에 크기·서버 밖 성공을 남김)
    b = store.last_metric("backup_kb")
    expect = today if now.hour >= 5 else today - timedelta(days=1)
    if not b:
        out.append(_check("DB 백업", "", "warn", "DB 백업 기록이 없어요",
                          "backup_db.sh가 백업 크기를 남기지 않았어요. 서버 crontab과 /root/lostkorea/backup.log를 보세요."))
    else:
        bday = date.fromisoformat(b["day"])
        off = m.get(b["day"], {}).get("backup_offsite_ok", 0)
        if bday < expect:
            out.append(_check("DB 백업", "", "warn", f"마지막 DB 백업이 {_kday(bday)}이에요",
                              "새벽 4시 백업이 빠졌어요. /root/lostkorea/backup.log와 '백업 실패' 메일을 보세요."))
        elif not off:
            out.append(_check("DB 백업", "", "warn", "서버 밖(R2) 백업이 안 됐어요",
                              f"{_kday(bday)} 서버 백업({int(b['n']):,}KB)은 됐지만 R2 올리기 기록이 없어요. backup.log를 보세요."))
        else:
            out.append(_check("DB 백업", f"{_kday(bday)} 04시, 서버와 서버 밖(R2) 모두 성공({int(b['n']):,}KB)."))

    # 알림 메일 재발송 대기·포기
    pend = store.list_alert_pending()
    gaveup = sum(v.get("alert_gaveup", 0) for d, v in m.items() if d >= (today - timedelta(days=7)).isoformat())
    if pend:
        p = min(pend, key=lambda r: (r.get("alert_pending") or {}).get("next") or "")
        info = p.get("alert_pending") or {}
        nxt = _kst(info.get("next"))
        when = f"다음 시도는 {_kdt(nxt)}({int(info.get('tries') or 0) + 1}번째)예요. " if nxt else ""
        out.append(_check("알림 메일", "", "warn", f"알림 메일 {len(pend)}건이 다시 보내기를 기다려요",
                          when + "24시간 동안 계속 실패하면 포기하고 운영 메일로 알려요."))
    elif gaveup:
        out.append(_check("알림 메일", "", "warn", f"최근 7일 알림 메일 {gaveup}건을 끝내 못 보냈어요",
                          "다섯 번 실패해 포기한 메일이에요. Maileroo 한도·SMTP 설정을 확인하세요."))
    else:
        out.append(_check("알림 메일", "다시 보낼 메일이 없어요."))

    # 디스크
    if disk is not None:
        if disk >= DISK_WARN:
            out.append(_check("디스크", "", "crit" if disk >= DISK_CRIT else "warn", f"디스크를 {disk}% 쓰고 있어요",
                              "다 차면 DB·검색·수집이 함께 멈춰요. df -h /, docker system df, du -sh /root/lostkorea-backups로 확인하세요."))
        else:
            out.append(_check("디스크", f"{disk}% 사용. {DISK_WARN}%를 넘으면 경보 메일."))

    # AI 모델(FIND-114: 하루 한도·모델 종료면 예비 모델로) — 이 서버 프로세스의 상태
    from . import gemini
    active = gemini._ACTIVE.get("model")
    if active:
        out.append(_check("AI 모델", "", "warn", f"AI가 예비 모델({active})로 돌고 있어요",
                          f"기본 모델({settings.gemini_grade_model})의 하루 한도를 넘었거나 모델이 종료됐어요. 서비스는 계속되지만 "
                          "단가가 더 높아요. 한도는 오후 5시쯤 풀리고, backend를 재시작하면 기본 모델로 돌아가요."))
    else:
        out.append(_check("AI 모델", f"기본 모델({settings.gemini_grade_model})로 응답해요."))

    # 오늘 AI 사용 상한(메모리 카운터 — 재시작하면 0부터)
    used = limiter.used_today if limiter._day == today.isoformat() else 0
    cap = settings.daily_ai_budget
    if cap and used >= cap * 0.8:
        out.append(_check("AI 사용 상한", "", "warn", f"오늘 AI 사용 상한의 {used * 100 // cap}%를 썼어요",
                          f"{cap:,} 단위를 넘으면 신고가 자정까지 막혀요(429). 서버가 재시작하면 0부터 다시 세요."))
    else:
        out.append(_check("AI 사용 상한", f"오늘 {cap:,} 단위 중 {used:,} 사용(서버가 켜진 뒤부터 셈)."))

    # 앱 오류(최근 이틀)
    err = recent("error")
    if err:
        out.append(_check("앱 오류", "", "warn", f"최근 이틀 앱 오류 {err}건",
                          "운영 메일로 간 오류 알림(같은 종류는 한 시간에 한 통)을 확인하세요."))
    else:
        out.append(_check("앱 오류", "최근 이틀 없음."))

    # 운영 화면 비밀번호 실패(최근 이틀)
    fail = recent("admin_auth_fail")
    if fail >= 3:
        out.append(_check("운영 화면 로그인", "", "warn", f"최근 이틀 운영 화면 비밀번호가 {fail}번 틀렸어요",
                          f"본인이 아니라면 ADMIN_PASSWORD를 바꾸세요. 같은 IP는 한 시간에 {settings.limit_admin_fail_hour}번 틀리면 막혀요."))
    else:
        out.append(_check("운영 화면 로그인", f"최근 이틀 비밀번호 실패 {fail}번."))

    order = {"crit": 0, "warn": 1}
    return sorted(out, key=lambda c: order.get(c["level"], 2))


# ── /admin/data ──
def build_data(now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    today = now.date()
    since = today - timedelta(days=SPAN_MAX * 2)
    m = store.metrics_by_day(since.isoformat())

    idx, idx_err, new_today = None, "", None
    try:
        idx = ops.index_status()
        new_today = _os("_count", {"query": {"term": {"indexed_at": today.isoformat()}}})["count"]
    except Exception as e:  # noqa: BLE001 — 검색 엔진이 죽어도 화면은 열려야(그게 '확인할 것' 1번)
        idx_err = type(e).__name__
    try:
        disk = ops._disk_percent()
    except OSError:
        disk = None

    # 신고 대장 — 기간 시작(KST 자정)부터 지금까지
    start_utc = datetime(since.year, since.month, since.day, tzinfo=KST).astimezone(timezone.utc).isoformat()
    ledger_from = (today - timedelta(days=SPAN_MAX)).isoformat()
    ledger = [r for r in (ledger_row(x) for x in store.reports_since(start_utc)) if r["day"] >= ledger_from]

    rc = store.report_counts()
    alerting = sum(1 for r in store.list_watching() if r.get("email_verified"))
    month = today.replace(day=1).isoformat()
    mail = sum(v.get("alert_sent", 0) + v.get("email_given", 0) for d, v in m.items() if d >= month)
    b = store.last_metric("backup_kb")

    return {
        "read_at": f"{now.month}월 {now.day}일 {now:%H:%M}",
        "today": today.isoformat(),
        "checks": build_checks(now, m, idx, idx_err, new_today, disk),
        "metrics": m,
        "first_metric_day": store.first_day("metrics"),
        "closures": store.closures_by_day(since.isoformat()),
        "first_closure_day": store.first_day("found_closures"),
        "our_closed": store.our_closed_days(since.isoformat()),
        "ledger": ledger,
        "now": {
            "reports": {k: rc.get(k, 0) for k in ("open", "candidate", "confirmed", "expired")} | {"alerting": alerting},
            "index": None if idx is None else {"count": idx.get("count"), "cap": CATCHUP_CAP,
                                               "latest": (idx.get("latest_found_at") or "")[:10]},
            "mail": {"month": today.month, "n": mail, "cap": MAIL_FREE_MONTH},
            "server": {"disk": disk, "backup_day": (b or {}).get("day"), "backup_kb": (b or {}).get("n"),
                       "offsite_ok": bool(b and m.get(b["day"], {}).get("backup_offsite_ok"))},
        },
        "ai_price": {"in": settings.ai_price_in_per_m, "out": settings.ai_price_out_per_m},
        "grade": {"strong": settings.rematch_grade_threshold, "weak": settings.weak_grade_threshold},
    }


# ── 신고 한 건 열기 ──
_EX_ROWS = (("item", "물건"), ("brand", "브랜드"), ("color", "색"), ("category", "대분류"),
            ("place_context", "장소·교통편"), ("lost_date", "잃어버린 날"), ("features", "특징"))


def _cands(row: dict) -> tuple[list[dict], int]:
    matches = [m for m in (row.get("matches") or []) if isinstance(m, dict)]
    best = row.get("best_match") or None
    key = lambda m: (m.get("atc_id"), str(m.get("fd_sn") or "1"))  # noqa: E731
    if best and row.get("status") == "confirmed" and key(best) not in {key(m) for m in matches}:
        matches = [best] + matches  # 약한 후보를 '내 물건이에요'로 고른 경우 목록에 없을 수 있음
    dismissed = set(row.get("dismissed") or [])
    hist = _history(row)
    out = []
    for m in sorted(matches, key=lambda m: -(m.get("grade") if isinstance(m.get("grade"), int) else -1)):
        g = m.get("grade") if isinstance(m.get("grade"), int) else None
        tag = None
        if best and key(m) == key(best) and row.get("status") == "confirmed":
            tag = ["yes", "'내 물건이에요'를 누름"]
        elif m.get("atc_id") in dismissed or f"{m.get('atc_id')}_{m.get('fd_sn') or '1'}" in dismissed:
            tag = ["no", "'내 물건이 아니었어요'를 누름" if "not_mine" in hist and best and key(m) == key(best)
                   else "'아니에요'로 뺌"]
        out.append({"grade": g, "name": m.get("name") or m.get("subject") or "",
                    "org": m.get("org_name") or "", "found": _kday(m.get("found_at")) if m.get("found_at") else "",
                    "ref": f"{m.get('atc_id', '')}-{m.get('fd_sn') or 1}", "reason": m.get("reason") or "", "tag": tag})
    return out[:3], len(out)


_STAGE_LABEL = {"contacted": "연락했어요", "visited": "방문했어요", "found": "찾았어요", "not_mine": "내 물건이 아니었어요"}


def _events(row: dict, image: bool) -> tuple[list[list[str]], list[str]]:
    """(시각이 있는 진행 기록, 시각을 기록하지 않는 사실)."""
    created, matched = _kst(row.get("created_at")), _kst(row.get("matched_at"))
    status, grades = row.get("status"), _grades(row)
    ev: list[tuple[datetime, str]] = []
    if created:
        ev.append((created, "사진으로 신고" if image else "글로 신고"))
    n = len(row.get("matches") or [])
    if matched and status == "candidate":
        early = created and abs((matched - created).total_seconds()) < 600
        ev.append((matched, f"{'유력 후보 제시' if early else '재매칭에서 유력 후보'}(점수 {row.get('best_grade')})"))
    elif matched and status == "confirmed":
        ev.append((matched, "'내 물건이에요'"))
    elif created and n and not any(g >= settings.rematch_grade_threshold for g in grades):
        ev.append((created, f"참고 후보 {n}개, 유력 후보 없음"))
    for h in (row.get("outcome") or {}).get("history") or []:
        at = _kst(h.get("at"))
        if at and h.get("stage") in _STAGE_LABEL:
            ev.append((at, _STAGE_LABEL[h["stage"]]))
    pending = row.get("alert_pending") or {}
    nxt = _kst(pending.get("next"))
    if nxt:
        ev.append((nxt, f"알림 메일 다시 보내기 예정({int(pending.get('tries') or 0) + 1}번째)"))
    if status == "expired":
        from apps.agent.rematch import watch_deadline
        ev.append((watch_deadline(row).astimezone(KST), "알림 기간 끝(만료)"))
    ev.sort(key=lambda x: x[0])

    untimed = []
    if row.get("email_verified"):
        untimed.append("이메일 확인, 알림 켬")
    if row.get("weak_notified"):
        untimed.append(f"알림 메일 보낸 후보 {len(row['weak_notified'])}개")
    if row.get("dismissed"):
        untimed.append(f"'아니에요'로 뺀 후보 {len(row['dismissed'])}개")
    return [[_kdt(at), label, at.isoformat()] for at, label in ev], untimed


def report_detail(row: dict) -> dict:
    from apps.agent.rematch import watch_deadline

    image = is_image_report(row)
    text = row.get("text") or ""
    lang = text_lang(text, row.get("lang") or "ko")
    ex = row.get("extracted") or {}
    created = _kst(row.get("created_at"))
    deadline = watch_deadline(row).astimezone(KST)
    email = ("확인됨, 알림 켬" if row.get("email_verified") else
             "입력했지만 아직 확인 안 함" if row.get("email") else "없음")
    cands, n_cands = _cands(row)
    events, untimed = _events(row, image)
    return {
        "rid": row["id"], "image": image, "lang": lang, "lang_label": LANG_LABEL.get(lang, lang),
        "text": text, "needs_ko": lang != "ko" and not image, "text_ko": row.get("text_ko"),
        "masked": bool(_MASK.search(text)),
        "extracted": [[label, (ex.get(k) or "").strip() or "없음"] for k, label in _EX_ROWS]
                     + [["검색 지역", ", ".join(row.get("region_set") or []) or "없음"]],
        "info": [["신고 시각", _kdt(created)], ["신고 번호", row["id"][:8]],
                 ["화면 언어", LANG_LABEL.get((row.get("lang") or "ko").split("-")[0], row.get("lang") or "ko")],
                 ["알림 이메일", email], ["알림 기간", f"{_kday(deadline.date())}까지"],
                 ["자동 삭제", f"{_kday((deadline + timedelta(days=settings.retention_days)).date())} 예정"]],
        "cands": cands, "cand_count": n_cands, "events": events, "untimed": untimed,
        "views": [_kdt(_kst(v)) for v in store.admin_views(row["id"])],
    }


_KO_PROMPT = """아래는 분실물 찾기 서비스에 들어온 신고 문장입니다. 운영자가 읽을 수 있게 자연스러운 한국어로 번역하세요.
규칙:
- 번역만 하세요. 설명·요약·추측을 덧붙이지 말 것(원문에 없는 색·브랜드·장소·특징 금지).
- [전화번호] [이메일] 같은 대괄호 가림 표시는 그대로 두세요.
- 역·기관·브랜드 이름은 한국에서 쓰는 표기로(예: Hongik Univ. Station → 홍대입구역), 숫자·모델명은 그대로.
반드시 JSON 하나로만: {{"ko": "<번역>"}}

신고 문장(JSON 문자열):
{text}"""


def translate_ko(text: str) -> str | None:
    """외국어 신고 문장 → 한국어. 실패·한글이 없는 결과면 None(화면이 '다시 시도'를 보여 줌)."""
    from . import gemini

    res = gemini.generate_json(_KO_PROMPT.format(text=json.dumps(text[:2000], ensure_ascii=False)))
    ko = res.get("ko") if isinstance(res, dict) else None
    if not isinstance(ko, str) or not _SCRIPTS["ko"].search(ko):
        return None
    return ko.strip()[:4000]


# ── 주소 ──
@router.get("/admin", dependencies=[Depends(require_admin)])
def admin_page() -> HTMLResponse:
    from .site import VERSION

    return HTMLResponse(_PAGE.read_text(encoding="utf-8").replace("{{V}}", VERSION), headers=_NO_STORE)


@router.get("/admin/data", dependencies=[Depends(require_admin), Depends(_require_xhr)])
def admin_data() -> JSONResponse:
    return JSONResponse(build_data(), headers=_NO_STORE)


def _row(rid: str) -> dict:
    row = store.get(rid) if re.fullmatch(r"[0-9a-f]{32}", rid or "") else None
    if not row:
        raise HTTPException(status_code=404, detail="신고가 없어요(지워졌을 수 있어요).", headers=_NO_STORE)
    return row


@router.get("/admin/reports/{rid}", dependencies=[Depends(require_admin), Depends(_require_xhr)])
def admin_report(rid: str) -> JSONResponse:
    row = _row(rid)
    out = report_detail(row)          # 이번 열람 전까지의 기록을 보여 주고
    store.log_admin_view(rid, "open")  # 이번 열람을 남김
    return JSONResponse(out, headers=_NO_STORE)


SEARCH_MAX = 100   # 검색 결과 상한(넘으면 '더 좁혀 찾기' 안내)


@router.post("/admin/reports/search", dependencies=[Depends(require_admin), Depends(_require_xhr)])
async def admin_search(request: Request) -> JSONResponse:
    """신고 찾기(FIND-125) — 문의가 오면 그 신고를 바로. 기간과 상관없이 남아 있는 신고 전부에서.

    결과는 대장 줄과 같은 모양(신고 문장·이메일 없음) — 내용은 줄을 열 때만, 연 기록은 그대로 남음.
    검색어는 저장·기록하지 않음(POST 본문이라 접속 기록에도 안 남음).
    """
    limiter.hit(f"adminsearch:{client_ip(request)}", 300, _HOUR)
    try:
        q = str((await request.json()).get("q") or "").strip()
    except Exception:  # noqa: BLE001
        q = ""
    if not 1 <= len(q) <= 100:
        raise HTTPException(status_code=400, detail="찾을 말을 1~100자로 넣어 주세요.", headers=_NO_STORE)
    rows = store.search_reports(q, SEARCH_MAX + 1)
    return JSONResponse({"rows": [ledger_row(r) for r in rows[:SEARCH_MAX]], "more": len(rows) > SEARCH_MAX},
                        headers=_NO_STORE)


@router.post("/admin/reports/{rid}/ko", dependencies=[Depends(require_admin), Depends(_require_xhr)])
def admin_report_ko(rid: str, request: Request) -> JSONResponse:
    """외국어 신고의 한국어 번역 — 저장된 게 있으면 그대로, 없으면 AI 한 번 부르고 신고에 저장(다음부턴 호출 없음)."""
    row = _row(rid)
    if row.get("text_ko"):
        return JSONResponse({"text_ko": row["text_ko"]}, headers=_NO_STORE)
    if text_lang(row.get("text") or "", row.get("lang") or "ko") == "ko" or is_image_report(row):
        return JSONResponse({"text_ko": None}, headers=_NO_STORE)
    limiter.hit(f"adminko:{client_ip(request)}", 60, _HOUR)
    limiter.spend(1)
    ko = translate_ko(row.get("text") or "")
    if not ko:
        raise HTTPException(status_code=502, detail="번역하지 못했어요. 잠시 뒤 다시 시도하세요.", headers=_NO_STORE)
    store.set_text_ko(rid, ko)
    return JSONResponse({"text_ko": ko}, headers=_NO_STORE)
