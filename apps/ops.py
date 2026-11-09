"""운영 점검·알림 — cron 스크립트가 호출. "조용히 죽는" 상태를 관리자에게 메일로 알림.

실행(서버, backend 이미지로):
  docker compose -f docker-compose.prod.yml run --rm -T backend uv run python -m apps.ops alert "제목" "본문"
  docker compose -f docker-compose.prod.yml run --rm -T backend uv run python -m apps.ops freshness --max-age-days 3

- alert: 관리자 주소(NOTIFY_EMAIL_TO, 없으면 SMTP_USER)로 메일. SMTP 미설정이면 콘솔 출력만.
- freshness: 색인의 가장 최근 습득일이 N일보다 오래되면(수집이 멈춘 신호) 알림 + 종료코드 1.
- weekly: 지난 7일 운영 요약(방문·신고·알림·오류·수집) 메일 — daily_collect.sh가 월요일에 호출.
- report_error: 앱 내부 예외를 관리자에게(같은 종류는 1시간에 1통) — Sentry 대용.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone

from apps.api.config import settings
from apps.notifier import EmailNotifier, _emit

KST = timezone(timedelta(hours=9))


def alert(subject: str, body: str) -> bool:
    to = settings.notify_email_to or settings.smtp_user
    _emit(f"[ops] {subject}\n{body}")
    if not (settings.smtp_user and settings.smtp_password and to):
        return False
    html = "<pre style=\"font-family:monospace;white-space:pre-wrap\">" + \
        body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return EmailNotifier._send(to, f"[FindIt 운영] {subject}", body, html)


_ERR_LAST: dict[str, float] = {}
_ERR_LOCK = threading.Lock()
ERROR_ALERT_INTERVAL = 3600  # 같은 오류는 1시간에 한 번만 메일


def report_error(where: str, exc: BaseException) -> bool:
    """앱 예외 → 통계(error) + 관리자 메일(종류별 1시간 1통, 백그라운드 발송). 메일을 보냈으면 True."""
    from apps import store

    store.bump("error")
    if settings.app_env != "production":  # 로컬 개발 중 오류로 관리자 메일이 나가지 않게
        return False
    key = f"{where}:{type(exc).__name__}"
    now = time.monotonic()
    with _ERR_LOCK:
        if now - _ERR_LAST.get(key, -ERROR_ALERT_INTERVAL) < ERROR_ALERT_INTERVAL:
            return False
        _ERR_LAST[key] = now
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    body = (f"위치: {where}\n시각: {datetime.now(KST):%Y-%m-%d %H:%M} KST\n\n{tb}\n"
            "(같은 오류는 1시간 동안 다시 보내지 않음)")
    threading.Thread(target=alert, args=(f"오류 — {where}: {type(exc).__name__}", body), daemon=True).start()
    return True


def index_status() -> dict:
    """색인 문서 수 + 가장 최근 습득일."""
    base = f"{settings.opensearch_url}/{settings.opensearch_index}"
    count = json.load(urllib.request.urlopen(f"{base}/_count", timeout=15))["count"]
    req = urllib.request.Request(
        f"{base}/_search",
        json.dumps({"size": 1, "sort": [{"found_at": {"order": "desc", "missing": "_last"}}],
                    "_source": ["found_at"]}).encode(),
        {"Content-Type": "application/json"},
    )
    hits = json.load(urllib.request.urlopen(req, timeout=15))["hits"]["hits"]
    latest = hits[0]["_source"].get("found_at", "") if hits else ""
    return {"count": count, "latest_found_at": latest}


def freshness(max_age_days: int, status: dict | None = None, today: date | None = None) -> bool:
    """최근 습득일이 max_age_days 이내면 True. 아니면 알림."""
    status = status or index_status()
    today = today or datetime.now(KST).date()
    latest = status.get("latest_found_at") or ""
    try:
        age = (today - date.fromisoformat(latest[:10])).days
    except ValueError:
        age = None
    ok = age is not None and age <= max_age_days
    print(f"[ops] 색인 {status.get('count')}건, 최근 습득일 {latest or '-'} ({age if age is not None else '?'}일 전) → "
          f"{'정상' if ok else '정체'}")
    if not ok:
        alert("데이터 정체 — 새 습득물이 들어오지 않아요",
              f"색인 문서 {status.get('count')}건, 가장 최근 습득일 {latest or '없음'}"
              f"({age if age is not None else '?'}일 전, 기준 {max_age_days}일).\n"
              "수집 cron·공공데이터 API·임베딩 쿼터를 확인하세요. 로그: /root/findit/daily_collect.log")
    return ok


_KIND_LABELS = [
    ("view_ko", "방문(한국어)"), ("view_en", "방문(영어)"), ("view_guide", "영어 가이드 방문"),
    ("report_text", "신고(글)"), ("report_image", "신고(사진)"), ("report_invalid", "신고 아님(잡담 등)"),
    ("email_given", "이메일 입력"), ("email_verified", "이메일 확인"), ("alert_sent", "알림 메일 발송"),
    ("confirm", "'내 물건이에요'"), ("dismiss", "'아니에요'"), ("delete", "신고 삭제"),
    ("browse", "둘러보기 조회"), ("search_api", "검색 API"), ("rate_limited", "요청 제한(429)"), ("error", "오류"),
]


def weekly(days: int = 7, today: date | None = None, status: dict | None = None) -> str:
    """지난 days일 운영 요약 → 관리자 메일. 본문을 반환."""
    from apps import store

    today = today or datetime.now(KST).date()
    since = (today - timedelta(days=days)).isoformat()
    m = store.metrics_since(since)
    rc = store.report_counts()
    try:
        status = status or index_status()
    except Exception as e:  # noqa: BLE001
        status = {"count": f"조회 실패({e})", "latest_found_at": "-"}
    lines = [f"기간: {since} ~ {today} ({days}일)", ""]
    lines += [f"  {label}: {m.get(kind, 0):,}" for kind, label in _KIND_LABELS]
    lines += ["", "신고 현황(전체):",
              f"  찾는 중 {rc.get('open', 0)} · 유력 후보 {rc.get('candidate', 0)} · 확인 {rc.get('confirmed', 0)}"
              f" · 만료 {rc.get('expired', 0)} · 이메일 확인됨 {rc.get('email_verified', 0)}",
              "", f"습득물 색인: {status.get('count')}건, 최근 습득일 {status.get('latest_found_at') or '-'}"]
    body = "\n".join(lines)
    print(body)
    alert(f"주간 운영 요약 ({since}~{today})", body)
    return body


def main() -> None:
    ap = argparse.ArgumentParser(description="FindIt 운영 점검·알림")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("alert")
    a.add_argument("subject")
    a.add_argument("body", nargs="?", default="")
    f = sub.add_parser("freshness")
    f.add_argument("--max-age-days", type=int, default=3)
    w = sub.add_parser("weekly")
    w.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    if args.cmd == "alert":
        alert(args.subject, args.body)
    elif args.cmd == "weekly":
        weekly(args.days)
    elif not freshness(args.max_age_days):
        sys.exit(1)


if __name__ == "__main__":
    main()
