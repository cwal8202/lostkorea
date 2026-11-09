"""앱 설정 — .env 를 pydantic-settings 로 읽음. 시크릿은 코드에 없음."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]  # repo 루트


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ROOT / ".env"), extra="ignore", case_sensitive=False
    )

    app_env: str = "dev"              # production이면 앱 오류를 관리자에게 메일(로컬 개발 중 오류 메일 방지)
    gemini_api_key: str
    gemini_embed_model: str = "gemini-embedding-001"
    gemini_embed_dim: int = 768
    gemini_grade_model: str = "gemini-3.6-flash"  # 2.5-flash는 신규차단(404) → 3.6-flash
    # 기본 모델이 404(종료·차단)면 이 모델로 자동 전환 + 관리자 메일(2.5-flash 차단 때처럼 서비스가 멈추지 않게). 같은 가격.
    gemini_fallback_model: str = "gemini-3.8-flash"
    # 모델의 숨은 '생각(thinking)' 토큰 — 출력 요금으로 청구. 실측(2026-11-09): 신고 1건 출력 토큰의 80%가 thinking인데
    # 꺼도 추출 20/20·노선 8/10·유력 후보 판정 95/96으로 켠 것과 같음 → minimal. 빈 값이면 모델 기본값(켜짐).
    gemini_thinking_level: str = "minimal"
    # 주간 요약의 AI 비용 추정용 단가(USD / 100만 토큰, 출력은 thinking 포함). Gemini 3.6 Flash 유료 등급 2026년 가격 —
    # 2027-01-01부터 2배 예정(가격표 확인 후 .env로 갱신).
    ai_price_in_per_m: float = 0.75
    ai_price_out_per_m: float = 3.75
    kakao_rest_api_key: str = ""
    data_go_kr_service_key_decoded: str = ""  # 경찰청 습득물 API (디코딩 키)

    opensearch_url: str = "http://localhost:9200"
    opensearch_index: str = "found_items"

    # 분실물 저장소(린 v1: SQLite. 추후 PostgreSQL). 런타임 상태 → git 제외.
    db_path: Path = _ROOT / "data" / "lostkorea.db"
    rematch_grade_threshold: int = 80  # 이 점수 이상이면 "매칭 성립"(알림 대상)
    weak_grade_threshold: int = 50     # 약한 후보 하한 — 신고자가 옵트인하면 [이 값, 위 임계) 구간도 '참고' 알림
    watch_days_default: int = 30       # 지속 재매칭(알림) 기간 기본값 — 지나면 expired, 재매칭·LLM 비용 중단
    watch_days_max: int = 180          # 선택 상한 = 습득물 최대 보관 기간(~6개월)
    found_retention_days: int = 185   # 습득일이 이보다 오래된 습득물은 색인에서 삭제(유실물법 6개월 + 여유)
    collect_workers: int = 4          # 상세 조회 동시 요청 수(공공 API 예의 — 너무 올리지 말 것)
    retention_days: int = 30          # 알림 기간이 끝난 뒤 이 일수가 지나면 신고 자동 삭제(개인정보 보유 기간)
    verify_hours: int = 48             # 이메일 확인 링크 유효 시간

    # 남용 방지 — IP별 레이트리밋 + 하루 전체 AI 사용량 상한(단일 프로세스 메모리 카운터, 재시작 시 초기화)
    limit_report_10min: int = 5        # 텍스트 신고(추출·임베딩·판정 LLM)
    limit_report_day: int = 30
    limit_image_10min: int = 3         # 사진 신고(Vision + 사진 대조, 더 비쌈)
    limit_image_day: int = 10
    limit_search_hour: int = 30        # 공개 검색 API(grade=true면 LLM)
    limit_translate_hour: int = 60     # 둘러보기 외국어 검색어 번역
    limit_alerts_hour: int = 10        # 알림 설정·이메일 확인
    limit_verify_mail_day: int = 3     # 같은 이메일 주소로 보내는 확인 메일
    daily_ai_budget: int = 1500        # 하루(KST) 전체 AI 요청 단위 상한 — 초과 시 429(과금 폭주 차단)

    # 알림 채널: console | email  (email은 아래 SMTP 설정 필요, 미설정 시 console 폴백)
    notifier_channel: str = "console"
    public_base_url: str = "https://findit-lost.duckdns.org"  # 메일 CTA·로고 URL 등 외부 링크 기준
    brand_name: str = "LostKorea"   # 사용자에게 보이는 서비스 이름(화면·메일·OG)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""            # 발신 계정(Gmail 주소)
    smtp_password: str = ""        # 앱 비밀번호(2단계 인증 후 발급)
    notify_email_to: str = ""      # 수신 주소(미지정 시 smtp_user)

    # gazetteer/지오코딩 데이터 위치 (현재 eval/data 재사용 — 추후 공용 data/로 이동 예정)
    data_dir: Path = _ROOT / "eval" / "data"


settings = Settings()
