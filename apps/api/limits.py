"""남용 방지 — IP·키별 레이트리밋 + 하루 전체 AI 사용량 상한.

외부 의존성 없이 메모리 카운터(슬라이딩 윈도). 운영은 uvicorn 단일 프로세스라 충분하고,
재시작 시 초기화된다(워커를 늘리면 Redis 등 공유 저장소로 바꿔야 함).

- hit(key, limit, window): 윈도 안 호출 수가 limit 이상이면 429(Retry-After 포함).
- spend(units): 하루(KST) AI 사용량 상한 — 초과 시 429. LLM·임베딩을 부르는 요청만 소비.
- client_ip(request): Caddy(내부망) 뒤에서는 X-Forwarded-For의 맨 오른쪽(프록시가 붙인 실제 IP)만 신뢰.
  그 IP가 Cloudflare 서버(프록시 켬)면 Cloudflare가 넣은 CF-Connecting-IP(실제 방문자)를 쓴다 — 아니면 모든 방문자가
  Cloudflare IP 몇 개로 묶여 서로의 한도를 나눠 쓰게 된다. 직접 접속한 사람이 이 헤더를 위조해도 Cloudflare IP가
  아니므로 무시된다.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .config import settings

KST = timezone(timedelta(hours=9))


class Limiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._day = ""
        self._used = 0

    def hit(self, key: str, limit: int, window: float, message: str = "요청이 너무 많아요. 잠시 후 다시 시도해주세요.") -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:  # limit 0(기능 끄기)이면 q가 비어 있어도 차단
                retry = int(window - (now - q[0])) + 1 if q else int(window)
                raise HTTPException(status_code=429, detail=message, headers={"Retry-After": str(retry)})
            q.append(now)

    def spend(self, units: int = 1) -> None:
        today = datetime.now(KST).date().isoformat()
        with self._lock:
            if today != self._day:
                self._day, self._used = today, 0
            if self._used + units > settings.daily_ai_budget:
                raise HTTPException(status_code=429,
                                    detail="오늘 AI 검색 이용량이 모두 소진됐어요. 내일 다시 이용해주세요.")
            self._used += units

    @property
    def used_today(self) -> int:
        return self._used


limiter = Limiter()


# Cloudflare 서버 IP 대역 — https://www.cloudflare.com/ips-v4 · ips-v6 (2026-11-09 받음, 거의 안 바뀜. 바뀌면 여기 갱신)
CLOUDFLARE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
))


def _is_cloudflare(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in CLOUDFLARE_NETS)


def _is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for", "")
    if not (xff and _is_private(peer)):  # 내부망 프록시(Caddy)가 붙인 값만 신뢰
        return peer
    ip = xff.split(",")[-1].strip() or peer  # 맨 오른쪽 = Caddy에 실제로 접속한 IP
    if _is_cloudflare(ip):  # Cloudflare 프록시를 거쳐 옴 → Cloudflare가 넣은 실제 방문자 IP
        cf = request.headers.get("cf-connecting-ip", "").strip()
        try:
            return str(ipaddress.ip_address(cf))
        except ValueError:
            return ip
    return ip
