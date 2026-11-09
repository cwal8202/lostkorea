"""테스트 공통 설정 — 외부 서비스(OpenSearch/Gemini/SMTP) 없이 순수 로직을 검증.

apps.* 임포트 전에 환경을 고정한다:
- GEMINI_API_KEY: 더미(모든 Gemini 호출은 테스트에서 목킹, 실제 호출 없음).
- DATABASE_URL: 비움 → store는 SQLite 백엔드.
- DB_PATH: 임시 파일 → 개발용 실제 DB(data/findit.db)를 건드리지 않음.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.pop("DATABASE_URL", None)  # SQLite 강제
# 실제 메일 발송 차단 — 로컬 .env에 SMTP가 있어도 테스트가 관리자·사용자에게 메일을 보내지 않게(환경변수가 .env보다 우선)
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["APP_ENV"] = "test"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="findit-test-"), "test.db")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_limits():
    """레이트리밋·일일 AI 상한은 프로세스 메모리 카운터 → 테스트끼리 섞이지 않게 매번 초기화."""
    from apps.api import search
    from apps.api.limits import limiter

    limiter.reset()
    search._KW_CACHE.clear()
    yield
    limiter.reset()
