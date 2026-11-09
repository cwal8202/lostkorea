"""운영 점검 — 데이터 정체(최근 습득일이 오래됨)면 관리자 알림. 외부 서비스 없음."""

from datetime import date

from apps import ops


def test_freshness_ok_no_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "alert", lambda s, b: sent.append(s))
    assert ops.freshness(3, {"count": 43743, "latest_found_at": "2026-10-11"}, today=date(2026, 10, 13))
    assert sent == []


def test_freshness_stale_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "alert", lambda s, b: sent.append(s))
    assert not ops.freshness(3, {"count": 5985, "latest_found_at": "2026-09-19"}, today=date(2026, 10, 13))
    assert len(sent) == 1 and "정체" in sent[0]


def test_freshness_empty_index_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(ops, "alert", lambda s, b: sent.append(s))
    assert not ops.freshness(3, {"count": 0, "latest_found_at": ""}, today=date(2026, 10, 13))
    assert sent


def test_prod_compose_has_no_default_db_password():
    """공개 레포에 기본 비밀번호가 다시 들어가지 않게 — 운영 compose는 POSTGRES_PASSWORD 필수(:?), 기본값 금지."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "findit_pw" not in text and ":-" not in text.split("POSTGRES_PASSWORD")[1][:3]
    assert text.count("${POSTGRES_PASSWORD:?") == 2       # postgres 환경변수 + 백엔드 DATABASE_URL
    assert "name: lostkorea" in text and "container_name: findit" not in text
