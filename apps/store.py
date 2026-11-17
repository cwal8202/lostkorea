"""분실물 저장소 — SQLite / PostgreSQL 이중 백엔드.

- `DATABASE_URL`(postgres://...) 있으면 PostgreSQL, 없으면 SQLite(로컬 개발).
- 인터페이스(init/add/get/list_open/update_match/update_alerts/confirm/dismiss/...)는 두 백엔드 동일.
- status: open(찾는중) | candidate(유력 후보) | confirmed(확인) | expired(알림 기간 만료 — 재매칭 중단).
- JSON 컬럼은 TEXT에 json.dumps로 저장(두 DB 공통). 개인정보 포함 → DB는 git 제외.
- 보안: 신고 관리 토큰·이메일 확인 토큰은 **SHA-256 해시만** 저장(DB 유출 시에도 조작 불가).
  이메일은 확인(double opt-in) 전까지 email_verified=0 → 알림 발송 안 함.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if _PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3

    from apps.api.config import settings

_JSON_COLS = ("extracted", "region_set", "queries", "best_match", "dismissed", "weak_notified", "matches", "graded_ids",
              "link_hashes", "alert_pending", "outcome")


KST = timezone(timedelta(hours=9))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    """신고 id — 128비트 무작위(추측 불가)."""
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _conn():
    if _PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str) -> str:
    """SQL은 '?' 플레이스홀더로 작성 → Postgres면 '%s'로 변환."""
    return sql.replace("?", "%s") if _PG else sql


def _run(sql: str, params: tuple = ()) -> None:
    conn = _conn()
    try:
        conn.execute(_q(sql), params)
        conn.commit()
    finally:
        conn.close()


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = _conn()
    try:
        cur = conn.execute(_q(sql), params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _one(sql: str, params: tuple = ()) -> dict | None:
    conn = _conn()
    try:
        cur = conn.execute(_q(sql), params)
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def init() -> None:
    _run("""
        CREATE TABLE IF NOT EXISTS lost_items (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            email TEXT,
            text TEXT,
            extracted TEXT,
            region_set TEXT,
            queries TEXT,
            status TEXT DEFAULT 'open',
            best_match TEXT,
            best_grade INTEGER,
            dismissed TEXT,
            notify_weak INTEGER DEFAULT 0,
            weak_notified TEXT,
            watch_until TEXT,
            lang TEXT DEFAULT 'ko',
            token_hash TEXT,
            email_verified INTEGER DEFAULT 0,
            verify_hash TEXT,
            verify_expires TEXT,
            matches TEXT,
            graded_ids TEXT,
            link_hashes TEXT,
            alert_pending TEXT,
            outcome TEXT,
            created_at TEXT,
            matched_at TEXT
        )
    """)
    # 익명 운영 통계(일·종류별 횟수만 — IP·이메일·신고 내용 없음). 방문·신고·알림·오류 추이 확인용.
    _run("""
        CREATE TABLE IF NOT EXISTS metrics (
            day TEXT NOT NULL,
            kind TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, kind)
        )
    """)
    # 습득물 이탈 기록 — 매일 경찰 목록과 대조해 색인에서 지우거나(종결) 사진을 바꾸기(내려감) **전에** 남김.
    # 공공 습득물 정보만(물품 이름·특이사항 원문·연락처는 저장 안 함 — 이름에 주인 이름이 섞일 수 있음). 개인정보 없음.
    _run("""
        CREATE TABLE IF NOT EXISTS found_closures (
            item_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT,
            category_top TEXT,
            category_sub TEXT,
            color TEXT,
            region TEXT,
            dep_place TEXT,
            found_place TEXT,
            has_photo INTEGER,
            has_desc INTEGER,
            found_at TEXT,
            first_seen TEXT,
            detected_at TEXT,
            check_window TEXT,
            our_report TEXT,
            PRIMARY KEY (item_id, kind)
        )
    """)
    # 수령 진행(성과) 익명 기록 — 신고자가 '연락했어요·방문했어요·찾았어요·내 물건이 아니었어요'를 누르면 남김.
    # 신고를 지워도 회수율을 잴 수 있게 신고와 따로 둠. 신고 id는 해시로만(되돌릴 수 없음), 신고 문장·이메일·물품 이름 없음.
    _run("""
        CREATE TABLE IF NOT EXISTS outcomes (
            report_key TEXT NOT NULL,
            stage TEXT NOT NULL,
            at TEXT,
            report_day TEXT,
            lang TEXT,
            item_category TEXT,
            item_source TEXT,
            item_grade INTEGER,
            days_from_report INTEGER,
            PRIMARY KEY (report_key, stage)
        )
    """)
    # 운영 화면 열람 기록 — 운영자가 신고 내용(문장)을 연 시각. 신고 id는 해시로만(신고를 지워도 기록은 남되 되돌릴 수 없음).
    _run("""
        CREATE TABLE IF NOT EXISTS admin_views (
            at TEXT NOT NULL,
            report_key TEXT NOT NULL,
            action TEXT
        )
    """)
    # 기존 DB 누락 컬럼 마이그레이션 (운영 Postgres도 테이블이 이미 있어 CREATE로는 안 생김)
    added = {"email": "TEXT", "dismissed": "TEXT", "notify_weak": "INTEGER DEFAULT 0",
             "weak_notified": "TEXT", "watch_until": "TEXT", "lang": "TEXT DEFAULT 'ko'",
             "token_hash": "TEXT", "email_verified": "INTEGER DEFAULT 0", "verify_hash": "TEXT",
             "verify_expires": "TEXT", "matches": "TEXT", "graded_ids": "TEXT", "link_hashes": "TEXT",
             "alert_pending": "TEXT", "outcome": "TEXT", "text_ko": "TEXT"}
    if _PG:
        for col, typ in added.items():
            _run(f"ALTER TABLE lost_items ADD COLUMN IF NOT EXISTS {col} {typ}")
    else:
        conn = _conn()
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(lost_items)")]
            for col, typ in added.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE lost_items ADD COLUMN {col} {typ}")
            conn.commit()
        finally:
            conn.close()


def _to_dict(d: dict) -> dict:
    for k in _JSON_COLS:
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["notify_weak"] = bool(d.get("notify_weak"))
    d["email_verified"] = bool(d.get("email_verified"))
    return d


def add(text: str, extracted: dict, region_set: list, queries: list,
        best_match: dict | None, best_grade: int | None,
        status: str = "open", user_id: str = "anon", email: str = "",
        notify_weak: bool = False, watch_until: str | None = None, lang: str = "ko",
        token_hash: str | None = None, matches: list | None = None,
        graded_ids: list | None = None) -> str:
    """신고 저장. email은 확인 전(email_verified=0) 상태로 저장 — 확인돼야 알림이 감."""
    lid = new_id()
    _run(
        "INSERT INTO lost_items (id,user_id,email,text,extracted,region_set,queries,"
        "status,best_match,best_grade,notify_weak,watch_until,lang,token_hash,email_verified,matches,"
        "graded_ids,created_at,matched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lid, user_id, email, text, json.dumps(extracted, ensure_ascii=False),
         json.dumps(region_set, ensure_ascii=False), json.dumps(queries, ensure_ascii=False),
         status, json.dumps(best_match, ensure_ascii=False) if best_match else None,
         best_grade, int(notify_weak), watch_until, lang or "ko", token_hash, 0,
         json.dumps(matches or [], ensure_ascii=False),
         json.dumps(graded_ids, ensure_ascii=False) if graded_ids else None,
         _now(), _now() if status in ("candidate", "confirmed") else None),
    )
    return lid


def list_open() -> list[dict]:
    return [_to_dict(r) for r in _rows("SELECT * FROM lost_items WHERE status='open'")]


def list_watching() -> list[dict]:
    """매일 재매칭할 신고 — 찾는 중(open) + 유력 후보(candidate). 후보가 틀렸을 수 있어 사용자가 '내 물건이에요'·삭제하거나
    알림 기간이 끝날 때까지 계속 찾음(예전엔 candidate가 되면 재매칭이 멈췄음 — 운영 신고 44건 중 33건)."""
    return [_to_dict(r) for r in _rows("SELECT * FROM lost_items WHERE status IN ('open', 'candidate')")]


def set_alert_pending(lid: str, pending: dict | None) -> None:
    """알림 메일 재발송 대기 — {"weak", "tries", "next"(ISO)} 또는 None(없음)."""
    _run("UPDATE lost_items SET alert_pending=? WHERE id=?",
         (json.dumps(pending, ensure_ascii=False) if pending else None, lid))


def list_alert_pending() -> list[dict]:
    return [_to_dict(r) for r in _rows("SELECT * FROM lost_items WHERE alert_pending IS NOT NULL")]


def all_items() -> list[dict]:
    """운영·점검용(공개 API에서는 쓰지 않음 — 다른 사람 신고 노출 방지)."""
    return [_to_dict(r) for r in _rows("SELECT * FROM lost_items ORDER BY created_at DESC")]


def get(lid: str) -> dict | None:
    r = _one("SELECT * FROM lost_items WHERE id=?", (lid,))
    return _to_dict(r) if r else None


def get_many(lids: list[str]) -> list[dict]:
    if not lids:
        return []
    marks = ",".join("?" * len(lids))
    return [_to_dict(r) for r in _rows(f"SELECT * FROM lost_items WHERE id IN ({marks})", tuple(lids))]


def check_token(row: dict | None, token: str | None) -> bool:
    """신고 관리 토큰 검증(상수 시간 비교). 토큰이 없는 옛 신고는 변경 불가.

    등록 때 브라우저가 받은 토큰 외에, 알림 메일 링크에 넣은 '링크 토큰'(최근 LINK_KEEP개)도 같은 권한 —
    다른 기기에서 메일을 열어도 결과를 보고 '내 물건이에요'를 누를 수 있게.
    """
    if not row or not token or not row.get("token_hash"):
        return False
    h = hash_token(token)
    ok = hmac.compare_digest(row["token_hash"], h)
    for lh in row.get("link_hashes") or []:
        ok |= hmac.compare_digest(str(lh), h)
    return ok


LINK_KEEP = 10  # 신고당 유효한 메일 링크 토큰 수(오래된 메일의 링크부터 만료)


def _add_hash(lid: str, h: str, unsub: bool) -> None:
    """link_hashes에 추가 — 결과 링크용(접두어 없음)·수신 거부용('u:' 접두어) 각각 최근 LINK_KEEP개만."""
    row = get(lid) or {}
    cur = row.get("link_hashes") or []
    links = [x for x in cur if not str(x).startswith("u:")]
    unsubs = [x for x in cur if str(x).startswith("u:")]
    if unsub:
        unsubs = unsubs[-(LINK_KEEP - 1):] + ["u:" + h]
    else:
        links = links[-(LINK_KEEP - 1):] + [h]
    _run("UPDATE lost_items SET link_hashes=? WHERE id=?", (json.dumps(links + unsubs), lid))


def new_link_token(lid: str) -> str:
    """알림 메일에 넣을 신고 전용 링크 토큰 발급 — 원문은 메일에만, 서버엔 해시만(최근 LINK_KEEP개) 저장."""
    token = secrets.token_urlsafe(24)
    _add_hash(lid, hash_token(token), unsub=False)
    return token


def new_unsub_token(lid: str) -> str:
    """'알림 그만 받기' 전용 토큰 — 주소창(쿼리)에 실려 서버 기록에 남을 수 있어 알림 끄기 말고는 아무 권한 없음."""
    token = secrets.token_urlsafe(24)
    _add_hash(lid, hash_token(token), unsub=True)
    return token


def check_unsub(row: dict | None, token: str | None) -> bool:
    if not row or not token:
        return False
    h = "u:" + hash_token(token)
    ok = False
    for x in row.get("link_hashes") or []:
        ok |= hmac.compare_digest(str(x), h)
    return ok


def update_match(lid: str, best_match: dict, best_grade: int, status: str,
                 matches: list | None = None) -> None:
    if matches is None:
        _run("UPDATE lost_items SET best_match=?, best_grade=?, status=?, matched_at=? WHERE id=?",
             (json.dumps(best_match, ensure_ascii=False), best_grade, status, _now(), lid))
    else:
        _run("UPDATE lost_items SET best_match=?, best_grade=?, status=?, matched_at=?, matches=? WHERE id=?",
             (json.dumps(best_match, ensure_ascii=False), best_grade, status, _now(),
              json.dumps(matches, ensure_ascii=False), lid))


def set_matches(lid: str, matches: list) -> None:
    """메일에 실을 후보 목록(서버가 판정한 것만) 갱신."""
    _run("UPDATE lost_items SET matches=? WHERE id=?", (json.dumps(matches, ensure_ascii=False), lid))


def set_graded(lid: str, keys: list[str] | None) -> None:
    """재매칭에서 LLM 판정을 마친 후보 키(atc_id_fd_sn). None이면 초기화 → 다음 재매칭은 무조건 다시 판정.

    신고자가 설정을 바꾸면(약한 후보 옵트인·이메일 확인·'아니에요') 같은 후보라도 알림 판단이 달라지므로 초기화한다.
    """
    _run("UPDATE lost_items SET graded_ids=? WHERE id=?",
         (json.dumps(keys[-500:], ensure_ascii=False) if keys else None, lid))


def set_pending_email(lid: str, email: str, verify_hash: str | None, verify_expires: str | None) -> None:
    """새 이메일 등록 → 확인 전 상태. 빈 문자열이면 알림 이메일 해제."""
    _run("UPDATE lost_items SET email=?, email_verified=0, verify_hash=?, verify_expires=? WHERE id=?",
         (email or "", verify_hash, verify_expires, lid))


def verify_email(token: str) -> dict | None:
    """확인 링크의 토큰 → 해당 신고 이메일을 확인 처리. 만료·불일치면 None."""
    if not token:
        return None
    row = _one("SELECT * FROM lost_items WHERE verify_hash=?", (hash_token(token),))
    if not row:
        return None
    if row.get("verify_expires") and row["verify_expires"] < _now():
        return None
    _run("UPDATE lost_items SET email_verified=1, verify_hash=NULL, verify_expires=NULL, graded_ids=NULL WHERE id=?",
         (row["id"],))
    return get(row["id"])


def update_alerts(lid: str, notify_weak: bool, watch_until: str, status: str | None = None) -> None:
    """결과창 '알림 설정' 저장 — 약한 후보 옵트인·알림 기간(+만료 신고 재개 시 status). 이메일은 별도(확인 절차)."""
    if status is None:
        _run("UPDATE lost_items SET notify_weak=?, watch_until=?, graded_ids=NULL WHERE id=?",
             (int(notify_weak), watch_until, lid))
    else:
        _run("UPDATE lost_items SET notify_weak=?, watch_until=?, status=?, graded_ids=NULL WHERE id=?",
             (int(notify_weak), watch_until, status, lid))


def bump(kind: str, n: int = 1, day: str | None = None) -> None:
    """익명 통계 +n (KST 일 단위). 통계 실패가 요청을 막지 않게 예외는 삼킴."""
    day = day or datetime.now(KST).date().isoformat()
    try:
        _run("INSERT INTO metrics (day, kind, n) VALUES (?, ?, ?) "
             "ON CONFLICT (day, kind) DO UPDATE SET n = metrics.n + excluded.n", (day, kind, n))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics] {kind} 기록 실패: {e}")


def bump_many(counts: dict[str, int], day: str | None = None) -> None:
    """여러 통계를 한 번의 DB 연결로 +n(AI 토큰처럼 호출마다 여러 값을 쌓을 때)."""
    day = day or datetime.now(KST).date().isoformat()
    sql = _q("INSERT INTO metrics (day, kind, n) VALUES (?, ?, ?) "
             "ON CONFLICT (day, kind) DO UPDATE SET n = metrics.n + excluded.n")
    try:
        conn = _conn()
        try:
            for kind, n in counts.items():
                if n:
                    conn.execute(sql, (day, kind, int(n)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[metrics] {list(counts)} 기록 실패: {e}")


def metrics_since(day_from: str) -> dict[str, int]:
    """day_from(포함) 이후 종류별 합계."""
    rows = _rows("SELECT kind, SUM(n) AS n FROM metrics WHERE day >= ? GROUP BY kind", (day_from,))
    return {r["kind"]: int(r["n"]) for r in rows}


def report_counts() -> dict[str, int]:
    """운영 요약용 신고 현황(개수만)."""
    rows = _rows("SELECT status, COUNT(*) AS n, "
                 "SUM(CASE WHEN email_verified=1 THEN 1 ELSE 0 END) AS verified FROM lost_items GROUP BY status")
    out = {r["status"]: int(r["n"]) for r in rows}
    out["email_verified"] = sum(int(r["verified"] or 0) for r in rows)
    return out


# ── 습득물 이탈 기록(found_closures) ──
# kind: closed(경찰 목록에서 빠짐 = 종결 — 주인 반환뿐 아니라 이관·폐기 등도 포함) /
#       photo_removed(실제 사진 → '사진 없음') / retention_end(우리 보관 기간 185일 끝 — 마지막 대조 때까지 보관중, 관찰 종료).
# check_window: 감지한 대조 — recent(최근 7일, 매일) / older(오래된 구간, 며칠마다 순환) / catchup / retention.
#   감지일은 '이날 처음 빠진 것을 봤다'는 뜻 — older는 앞선 확인 뒤 며칠 사이 어느 날 빠졌는지 모름.
CLOSURE_COLS = ("item_id", "kind", "source", "category_top", "category_sub", "color", "region", "dep_place",
                "found_place", "has_photo", "has_desc", "found_at", "first_seen", "detected_at", "check_window",
                "our_report")


def record_closures(rows: list[dict]) -> int:
    """이탈 기록 저장(한 연결·한 커밋). 같은 (item_id, kind)는 처음 것만 남김. 실패는 예외로 — 호출부가 삭제를 보류."""
    if not rows:
        return 0
    sql = _q(f"INSERT INTO found_closures ({','.join(CLOSURE_COLS)}) VALUES ({','.join('?' * len(CLOSURE_COLS))}) "
             "ON CONFLICT (item_id, kind) DO NOTHING")
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.executemany(sql, [tuple(r.get(c) for c in CLOSURE_COLS) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def closure_counts_since(day_from: str) -> dict[str, int]:
    """day_from(포함, KST 날짜) 이후 감지한 이탈 기록 종류별 건수(주간 요약용)."""
    rows = _rows("SELECT kind, COUNT(*) AS n FROM found_closures WHERE detected_at >= ? GROUP BY kind", (day_from,))
    return {r["kind"]: int(r["n"]) for r in rows}


def report_item_keys() -> dict[str, str]:
    """우리 신고가 짚은 습득물 {atc_id_fd_sn: 'confirmed'|'candidate'} — 이탈 기록에 '우리 서비스와 연결' 표시용.

    confirmed = 신고자가 '내 물건이에요'를 누름, candidate = 유력 후보로 제시함. 신고 내용·이메일은 꺼내지 않음.
    """
    out: dict[str, str] = {}
    for r in _rows("SELECT status, best_match FROM lost_items "
                   "WHERE status IN ('confirmed', 'candidate') AND best_match IS NOT NULL"):
        try:
            m = json.loads(r["best_match"]) or {}
        except (TypeError, ValueError):
            continue
        key = f"{m.get('atc_id', '')}_{m.get('fd_sn') or '1'}"
        if m.get("atc_id") and out.get(key) != "confirmed":
            out[key] = r["status"]
    return out


OUTCOME_STAGES = ("contacted", "visited", "found", "not_mine")


def record_outcome(row: dict, stage: str, match: dict | None) -> tuple[dict, bool]:
    """수령 진행 기록 — 신고에는 현재 단계, outcomes 표에는 익명 기록(같은 신고·단계는 처음 것만).
    (갱신된 outcome, 이 단계를 처음 누른 것인지) 반환 — 같은 버튼을 여러 번 눌러도 통계는 한 번.

    익명 기록 칸: 신고 날짜(KST)·언어·물품 대분류·출처·판정 점수·신고 뒤 며칠 — 신고가 지워져도 개인을 알 수 없음.
    """
    now = datetime.now(timezone.utc)
    prev = row.get("outcome") or {}
    hist = [h for h in (prev.get("history") or []) if h.get("stage") != stage] + [{"stage": stage, "at": now.isoformat()}]
    outcome = {"stage": stage, "at": now.isoformat(), "atc_id": (match or {}).get("atc_id"), "history": hist}
    _run("UPDATE lost_items SET outcome=? WHERE id=?", (json.dumps(outcome, ensure_ascii=False), row["id"]))
    created = row.get("created_at") or now.isoformat()
    try:
        days = (now - datetime.fromisoformat(created)).days
    except ValueError:
        days = None
    cat = ((match or {}).get("category") or "").split(">")[0].strip()
    key = hashlib.sha256(("outcome:" + row["id"]).encode()).hexdigest()
    if _one("SELECT 1 AS x FROM outcomes WHERE report_key=? AND stage=?", (key, stage)):
        return outcome, False
    _run("INSERT INTO outcomes (report_key, stage, at, report_day, lang, item_category, item_source, item_grade, "
         "days_from_report) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (report_key, stage) DO NOTHING",
         (key, stage, now.isoformat(),
          datetime.fromisoformat(created).astimezone(KST).date().isoformat() if created else None,
          row.get("lang") or "ko", cat, (match or {}).get("source"), (match or {}).get("grade"), days))
    return outcome, True


def outcome_counts_since(day_from: str) -> dict[str, int]:
    """day_from(포함) 이후 누른 수령 진행 단계별 신고 수(주간 요약·성과 집계)."""
    rows = _rows("SELECT stage, COUNT(*) AS n FROM outcomes WHERE at >= ? GROUP BY stage", (day_from,))
    return {r["stage"]: int(r["n"]) for r in rows}


# ── 운영 화면(/admin) 읽기 ──
def reports_since(created_from: str) -> list[dict]:
    """created_from(UTC ISO, 포함) 이후 신고 — 최신순. 운영 화면 신고 대장용(공개 API에서는 쓰지 않음)."""
    return [_to_dict(r) for r in _rows("SELECT * FROM lost_items WHERE created_at >= ? ORDER BY created_at DESC",
                                       (created_from,))]


def metrics_by_day(day_from: str) -> dict[str, dict[str, int]]:
    """{day: {kind: n}} — day_from(포함, KST 날짜) 이후."""
    out: dict[str, dict[str, int]] = {}
    for r in _rows("SELECT day, kind, n FROM metrics WHERE day >= ?", (day_from,)):
        out.setdefault(r["day"], {})[r["kind"]] = int(r["n"])
    return out


def first_day(table: str) -> str | None:
    """기록이 시작된 날(KST 날짜) — 그 전 날짜는 '0'이 아니라 '기록 없음'으로 보이게."""
    col = {"metrics": "day", "found_closures": "detected_at"}[table]
    r = _one(f"SELECT MIN({col}) AS d FROM {table}")
    return (r or {}).get("d")


def closures_by_day(day_from: str) -> dict[str, dict[str, int]]:
    """{감지일: {kind: 건수}} — 습득물 이탈 기록(종결·사진 내려감·보관 기간 끝)."""
    out: dict[str, dict[str, int]] = {}
    for r in _rows("SELECT detected_at AS day, kind, COUNT(*) AS n FROM found_closures WHERE detected_at >= ? "
                   "GROUP BY detected_at, kind", (day_from,)):
        out.setdefault(r["day"], {})[r["kind"]] = int(r["n"])
    return out


def our_closed_days(day_from: str) -> list[str]:
    """우리 신고가 짚은(유력 후보·'내 물건이에요') 습득물이 경찰 목록에서 종결된 감지일 — 한 건에 한 줄."""
    return [r["day"] for r in _rows("SELECT detected_at AS day FROM found_closures WHERE kind='closed' "
                                    "AND our_report IN ('candidate', 'confirmed') AND detected_at >= ?", (day_from,))]


def last_metric(kind: str) -> dict | None:
    """그 종류의 가장 최근 기록 {day, n}(예: backup_kb) — 없으면 None."""
    return _one("SELECT day, n FROM metrics WHERE kind=? ORDER BY day DESC LIMIT 1", (kind,))


def set_text_ko(lid: str, text_ko: str) -> None:
    """외국어 신고 문장의 한국어 번역(운영 화면에서 처음 열 때 만든 것) — 신고와 함께 지워짐."""
    _run("UPDATE lost_items SET text_ko=? WHERE id=?", (text_ko, lid))


ADMIN_VIEW_KEEP_DAYS = 365  # 열람 기록 보관 기간


def _view_key(lid: str) -> str:
    return hashlib.sha256(("view:" + lid).encode()).hexdigest()


def log_admin_view(lid: str, action: str = "open") -> None:
    """운영자가 신고 내용을 연 기록 + 보관 기간 지난 기록 정리. 실패해도 화면은 열림."""
    now = datetime.now(timezone.utc)
    try:
        _run("INSERT INTO admin_views (at, report_key, action) VALUES (?, ?, ?)", (now.isoformat(), _view_key(lid), action))
        _run("DELETE FROM admin_views WHERE at < ?", ((now - timedelta(days=ADMIN_VIEW_KEEP_DAYS)).isoformat(),))
    except Exception as e:  # noqa: BLE001
        print(f"[admin] 열람 기록 실패: {e}")


def admin_views(lid: str, limit: int = 5) -> list[str]:
    """그 신고를 연 시각(UTC ISO, 최신순)."""
    return [r["at"] for r in _rows("SELECT at FROM admin_views WHERE report_key=? ORDER BY at DESC LIMIT ?",
                                   (_view_key(lid), limit))]


def delete(lid: str) -> None:
    """신고 완전 삭제(신고자 요청 또는 보유 기간 경과)."""
    _run("DELETE FROM lost_items WHERE id=?", (lid,))


def set_status(lid: str, status: str) -> None:
    _run("UPDATE lost_items SET status=? WHERE id=?", (status, lid))


def mark_weak_notified(lid: str, atc_id: str) -> list[str]:
    """약한 후보 알림을 보낸 습득물 기록 → 같은 후보로 재매칭마다 반복 알림 방지."""
    row = get(lid)
    sent = (row.get("weak_notified") if row else None) or []
    if atc_id and atc_id not in sent:
        sent.append(atc_id)
    _run("UPDATE lost_items SET weak_notified=? WHERE id=?",
         (json.dumps(sent, ensure_ascii=False), lid))
    return sent


def confirm(lid: str, match: dict) -> None:
    """사용자가 '내 물건이에요' → 확인됨(수령 단계). match는 서버가 저장한 후보에서 찾은 것."""
    _run(
        "UPDATE lost_items SET status='confirmed', best_match=?, best_grade=?, matched_at=? WHERE id=?",
        (json.dumps(match, ensure_ascii=False), match.get("grade"), _now(), lid),
    )


def dismiss(lid: str, atc_id: str) -> list[str]:
    """사용자가 '아니에요' → 그 후보 제외(재추천 방지), 다시 찾는중(open)."""
    row = get(lid)
    dis = (row.get("dismissed") if row else None) or []
    if atc_id and atc_id not in dis:
        dis.append(atc_id)
    _run("UPDATE lost_items SET status='open', dismissed=?, graded_ids=NULL WHERE id=?",
         (json.dumps(dis, ensure_ascii=False), lid))
    return dis
