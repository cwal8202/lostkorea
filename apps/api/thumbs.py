"""경찰 습득물 사진의 작은 미리보기 — 원본(한 장 최대 ~3MB)을 256px 정사각형 JPEG(~20KB)로 줄여 돌려줌.

왜: 전체 습득물 한 페이지(사진 10~30장)를 휴대폰으로 열면 원본만 수십 MB — 로밍 중인 외국인에겐 큰 부담.
- 허용 주소는 경찰민원24 습득물 사진 경로만(아무 주소나 대신 받아 주는 프록시가 되지 않게).
- 줄인 사진은 컨테이너 임시 폴더에 캐시(배포 때 비워져도 됨) + 응답에 긴 캐시 헤더 → Cloudflare가 저장해 두고 대신 응답.
- 줄이기에 실패하면 원본 주소로 넘김(화면에서 사진이 사라지지 않게). 크게 보기는 원본 그대로.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import urllib.request
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


def make(url: str) -> bytes:
    """원본을 받아 256px 정사각형(가운데 자르기)으로 — 캐시에 있으면 그대로."""
    f = CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.jpg"
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
