"""경찰 습득물 사진의 작은 미리보기 — 원본(한 장 최대 ~3MB)을 256px 정사각형 JPEG(~20KB)로 줄여 돌려줌.

왜: 전체 습득물 한 페이지(사진 10~30장)를 휴대폰으로 열면 원본만 수십 MB — 로밍 중인 외국인에겐 큰 부담.
- 허용 주소는 경찰민원24 습득물 사진 경로만(아무 주소나 대신 받아 주는 프록시가 되지 않게).
- 줄인 사진은 디스크에 캐시 + 응답에 긴 캐시 헤더 → Cloudflare가 저장해 두고 대신 응답.
- 줄이기에 실패하면 원본 주소로 넘김(화면에서 사진이 사라지지 않게). 크게 보기는 원본 그대로.

미리 만들기(FIND-122): 처음 보는 사진은 경찰 서버에서 받느라 운영 서버 기준 1.6~2.0초(11/19 실측) — 한 페이지가 늦게 뜸.
그래서 운영에선 캐시를 배포해도 남는 볼륨(THUMB_CACHE_DIR)에 두고, 밤 수집이 최신 습득물부터 미리 만들어 둔다(prefill).
색인에서 빠진 습득물(종결·보관 기간 끝·사진 바뀜)의 사진은 같은 밤에 지운다(sweep). 다 만들어도 7.2만 장 × ~11KB ≈ 0.8GB.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageOps

ALLOWED_HOST = "minwon24.police.go.kr"
ALLOWED_PREFIX = "/lost112/find/getOpenapiAttachFileImage/"
SIZE = 256                       # 화면 112px(PC)·84px(휴대폰)의 2~3배 화면 밀도까지
MAX_SRC = 12 * 1024 * 1024       # 원본 상한(실측 최대 ~3MB)
CACHE_DIR = Path(os.environ.get("THUMB_CACHE_DIR", "/tmp/lk-thumbs"))
_slots = threading.Semaphore(8)  # 동시에 받아 줄이는 수(경찰 서버 응답 ~3초 — 한 페이지 사진을 몇 초 안에)


def allowed(url: str) -> bool:
    p = urlparse(url or "")
    return (p.scheme == "https" and p.hostname == ALLOWED_HOST and p.path.startswith(ALLOWED_PREFIX)
            and "no_img" not in p.path)


def _name(url: str) -> str:
    return f"{hashlib.sha1(url.encode()).hexdigest()}.jpg"


def make(url: str) -> bytes:
    """원본을 받아 256px 정사각형(가운데 자르기)으로 — 캐시에 있으면 그대로."""
    f = CACHE_DIR / _name(url)
    if f.exists():
        return f.read_bytes()
    with _slots:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (LostKorea thumbnail)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(MAX_SRC + 1)
        if len(data) > MAX_SRC:
            raise ValueError("원본이 너무 큼")
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
