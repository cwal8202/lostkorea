"""경찰 습득물 사진의 작은 미리보기 — 원본(한 장 최대 ~3MB)을 256px 정사각형 JPEG(~20KB)로 줄여 돌려줌.

왜: 전체 습득물 한 페이지(사진 10~30장)를 휴대폰으로 열면 원본만 수십 MB — 로밍 중인 외국인에겐 큰 부담.
- 허용 주소는 경찰민원24 습득물 사진 경로만(아무 주소나 대신 받아 주는 프록시가 되지 않게).
- 줄인 사진은 디스크에 캐시 + 응답에 긴 캐시 헤더 → Cloudflare가 저장해 두고 대신 응답.
- 줄이기에 실패하면 원본 주소로 넘김(화면에서 사진이 사라지지 않게). 크게 보기는 원본 그대로.

미리 만들기(FIND-122): 처음 보는 사진은 경찰 서버에서 받느라 운영 서버 기준 1.6~2.0초(11/19 실측) — 한 페이지가 늦게 뜸.
그래서 운영에선 캐시를 배포해도 남는 볼륨(THUMB_CACHE_DIR)에 두고, 밤 수집이 최신 습득물부터 미리 만들어 둔다(prefill).
색인에서 빠진 습득물(종결·보관 기간 끝·사진 바뀜)의 사진은 같은 밤에 지운다(sweep). 다 만들어도 7.2만 장 × ~11KB ≈ 0.8GB.

연결 다시 쓰기(11/20): 사진마다 새로 연결하던 때는 밤 90분에 3,746장뿐. 운영 서버(헬싱키)에서 새 연결은 ① 암호 연결(TLS)
맺기가 때때로 4~10초(평소 ~0.3초) ② 원본(평균 1~3MB, 큰 건 9MB)이 새 연결에선 느리게 받아짐(먼 거리에서 TCP가 천천히 속도를
올림 — 5.7MB 9.7초, 데워진 연결에선 3.3MB 0.8초). 프로세스 안에서 연결 묶음(keep-alive)을 같이 써서 데워진 연결을 다시 쓴다.
운영 컨테이너 실측(동시 6장, 아직 안 받은 사진 27장씩 번갈아 두 번): 예전 0.96·1.44장/초 → 연결 묶음 2.17·5.28장/초.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import urllib3
from PIL import Image, ImageOps

ALLOWED_HOST = "minwon24.police.go.kr"
ALLOWED_PREFIX = "/lost112/find/getOpenapiAttachFileImage/"
SIZE = 256                       # 화면 112px(PC)·84px(휴대폰)의 2~3배 화면 밀도까지
MAX_SRC = 12 * 1024 * 1024       # 원본 상한(실측 최대 ~3MB)
CACHE_DIR = Path(os.environ.get("THUMB_CACHE_DIR", "/tmp/lk-thumbs"))
_slots = threading.Semaphore(8)  # 동시에 받아 줄이는 수(경찰 서버 응답 ~3초 — 한 페이지 사진을 몇 초 안에)
# 경찰 서버 연결 묶음 — 스레드끼리 같이 쓰고(안전), 동시에 받는 수(_slots·밤 workers)만큼 연결을 남겨 둠.
# 재시도는 urllib3에 맡기지 않음(시간 초과까지 다시 하면 방문자가 40초 기다림) — 끊긴 연결만 _fetch가 한 번 다시.
_POOL = urllib3.PoolManager(
    num_pools=2, maxsize=16, block=False,
    headers={"User-Agent": "Mozilla/5.0 (LostKorea thumbnail)"},
    timeout=urllib3.Timeout(connect=20, read=20),   # connect는 TLS 맺기까지(경찰 서버 최대 ~10초 실측)
    retries=urllib3.Retry(total=None, connect=0, read=0, redirect=3, status=0, other=0),
)


def allowed(url: str) -> bool:
    p = urlparse(url or "")
    return (p.scheme == "https" and p.hostname == ALLOWED_HOST and p.path.startswith(ALLOWED_PREFIX)
            and "no_img" not in p.path)


def _name(url: str) -> str:
    return f"{hashlib.sha1(url.encode()).hexdigest()}.jpg"


def _fetch(url: str) -> bytes:
    """원본 사진 바이트 — 연결 묶음에서 쉬고 있는 연결을 다시 씀(새 TLS 맺기 4~10초를 건너뜀)."""
    for attempt in (1, 2):
        try:
            r = _POOL.request("GET", url, preload_content=False)
        except urllib3.exceptions.MaxRetryError as e:
            # 오래 쉬던 연결을 경찰 서버가 먼저 끊었으면 새 연결로 한 번만 다시 — 시간 초과·접속 실패는 다시 안 함
            if attempt == 1 and isinstance(e.reason, urllib3.exceptions.ProtocolError):
                continue
            raise
        done = False
        try:
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            data = r.read(MAX_SRC + 1)
            if len(data) > MAX_SRC:
                raise ValueError("원본이 너무 큼")
            done = True
            return data
        finally:
            if not done:
                r.close()          # 다 안 읽은 연결은 묶음에 깨끗하지 않게 남지 않도록 닫음(다음엔 새로 연결)
            r.release_conn()
    raise OSError("사진을 받지 못함")


def make(url: str) -> bytes:
    """원본을 받아 256px 정사각형(가운데 자르기)으로 — 캐시에 있으면 그대로."""
    f = CACHE_DIR / _name(url)
    if f.exists():
        return f.read_bytes()
    with _slots:
        data = _fetch(url)
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (SIZE * 2, SIZE * 2))      # JPEG는 디코딩 단계에서 줄여 읽음(빠르고 메모리 적게)
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = ImageOps.fit(im, (SIZE, SIZE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
    out = buf.getvalue()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(out)
        tmp.replace(f)
    except OSError:
        pass
    return out


def prefill(urls: list[str], max_new: int, budget_s: float, workers: int = 4) -> dict:
    """캐시에 없는 사진을 앞에서부터(호출부가 최신순으로 줌) 최대 max_new장·budget_s초까지 미리 만든다.

    경찰 서버에 부담을 덜 주게 동시 workers장만. 실패한 사진은 건너뜀(볼 때 다시 시도·원본으로 넘김).
    """
    todo = [u for u in dict.fromkeys(urls) if allowed(u) and not (CACHE_DIR / _name(u)).exists()]
    st = {"missing": len(todo), "made": 0, "failed": 0}
    todo, start = todo[:max_new], time.monotonic()
    step = max(1, workers * 8)  # 이만큼씩 나눠 돌리며 시간 상한 확인

    def one(u: str) -> bool:
        try:
            make(u)
            return True
        except Exception:  # noqa: BLE001
            return False

    with ThreadPoolExecutor(workers) as ex:
        for i in range(0, len(todo), step):
            if time.monotonic() - start > budget_s:
                break
            ok = list(ex.map(one, todo[i : i + step]))
            st["made"] += sum(ok)
            st["failed"] += len(ok) - sum(ok)
    st["left"] = st["missing"] - st["made"]
    return st


def sweep(keep_urls: list[str], min_age_s: float = 3600, max_ratio: float = 0.5) -> dict:
    """색인에 없는 사진(종결·보관 기간 끝·사진 바뀜)의 작은 사진을 지운다.

    안전장치: 만든 지 min_age_s 안 된 파일은 두고(방금 화면이 만든 것), 지울 게 전체의 max_ratio를 넘으면
    색인 조회가 잘못된 것으로 보고 하나도 안 지움(파일이 1,000개 미만이면 이 확인은 건너뜀).
    """
    if not CACHE_DIR.is_dir():
        return {"files": 0, "removed": 0, "skipped": False}
    keep = {_name(u) for u in keep_urls}
    now = time.time()
    files = list(CACHE_DIR.iterdir())
    old = []
    for f in files:
        try:
            if f.name not in keep and now - f.stat().st_mtime > min_age_s:
                old.append(f)
        except OSError:
            pass
    if len(files) >= 1000 and len(old) > max_ratio * len(files):
        return {"files": len(files), "removed": 0, "skipped": True}
    removed = 0
    for f in old:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return {"files": len(files), "removed": removed, "skipped": False}
