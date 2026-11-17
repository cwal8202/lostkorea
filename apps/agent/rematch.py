"""지속 재매칭 — 저장된 미해결(찾는 중 open·유력 후보 candidate) 분실물을 현재 인덱스와 다시 대조.

수집기가 새 습득물을 색인한 뒤 호출(또는 스케줄러가 수집 후 실행).
저장된 추출·지역집합·쿼리를 재사용 → LLM 추출/라우팅 재호출 없이 검색+grade만(저렴).
상위 후보가 지난번 판정한 것과 같으면 grade도 생략(graded_ids) → 비용은 새 후보가 들어온 신고에만.
임계 grade 이상이면 유력 후보 → status=candidate, 알림(그 뒤에도 계속 찾고, 다른 유력 후보가 나오면 다시 알림).
신고자가 옵트인했으면 약한 후보도 '참고' 알림. 알림은 실제로 보냈을 때만 기록, 실패하면 재발송 대기(send_alert).
로그엔 신고 문장·이메일을 남기지 않음(id만).

실행:  uv run python -m apps.agent.rematch
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps import store
from apps.agent import graph
from apps.api.config import settings
from apps.notifier import notifier


def watch_until_after(days: int | None = None) -> str:
    """지금부터 days일 뒤(ISO). 1~watch_days_max로 클램프, 미지정이면 기본값."""
    d = days or settings.watch_days_default
    d = max(1, min(int(d), settings.watch_days_max))
    return (datetime.now(timezone.utc) + timedelta(days=d)).isoformat()


def watch_deadline(row: dict) -> datetime:
    """신고의 알림 마감 시각. watch_until 없는 기존 신고는 등록일 + 기본 기간으로 간주."""
    if row.get("watch_until"):
        return datetime.fromisoformat(row["watch_until"])
    created = datetime.fromisoformat(row.get("created_at") or datetime.now(timezone.utc).isoformat())
    return created + timedelta(days=settings.watch_days_default)


def alert_level(grade: int | None, notify_weak: bool, match: dict | None = None) -> str | None:
    """알림 등급 — 'strong'(유력 후보, ≥임계) | 'weak'(옵트인 시 [하한, 임계)) | None(알림 없음).

    약한 후보는 확정도 유력도 아니므로 상태를 open으로 두고(더 나은 후보 계속 탐색) 알림만 보낸다.
    판정이 장소 '안 이어짐'과 날짜 '어긋남'을 함께 적은 후보는 점수가 높아도 알림 없음 — 점수·순위는 그대로(화면엔 보임).
    실측(11/17): 서울 분실 ↔ 인천 습득 아이폰, 5월 분실 ↔ 7월 다른 동네 리락쿠마가 80~85점으로 '유력'이 됐음.
    """
    if grade is None:
        return None
    if match and match.get("link_place") == "안 이어짐" and match.get("link_date") == "어긋남":
        return None
    if grade >= settings.rematch_grade_threshold:
        return "strong"
    if notify_weak and grade >= settings.weak_grade_threshold:
        return "weak"
    return None


_SLIM_KEYS = ("atc_id", "fd_sn", "name", "subject", "description", "category", "color", "dep_place",
              "found_place", "region", "found_at", "org_name", "tel", "source", "image_url", "score", "grade", "reason",
              "name_en", "desc_en", "visual_score", "visual_reason", "link_place", "link_date")


def is_dismissed(m: dict, dismissed: set) -> bool:
    return m.get("atc_id") in dismissed or f"{m.get('atc_id')}_{m.get('fd_sn') or '1'}" in dismissed


def slim_matches(matches: list[dict], n: int = 12) -> list[dict]:
    """메일·확인용으로 저장할 후보(서버 판정 결과) — 상위 n개(판정한 12개 전부 — 다시 열어도 같은 후보), 필요한 필드만, 설명은 짧게."""
    out = []
    for m in (matches or [])[:n]:
        d = {k: m.get(k) for k in _SLIM_KEYS if m.get(k) is not None}
        if isinstance(d.get("description"), str):
            d["description"] = d["description"][:300]
        out.append(d)
    return out


def can_email(row: dict) -> bool:
    """알림 메일은 신고자가 확인(double opt-in)한 이메일에만."""
    return bool(row.get("email")) and bool(row.get("email_verified"))


# 알림 메일 재발송 간격(분) — 1차 실패 뒤 15분·1시간·6시간·24시간. 그래도 실패면 포기 + 관리자 알림.
# Maileroo 무료는 시간당 30통 — 밤 재매칭에서 알림이 몰려 한도에 걸려도 다음 시간대에 다시 나가게.
RETRY_MINUTES = (15, 60, 360, 1440)


def send_alert(row: dict, matches: list[dict], weak: bool = False, now: datetime | None = None) -> bool:
    """알림 메일 한 통 — 업체가 받았을 때만 alert_sent·'알린 후보' 기록. 실패면 재발송 대기(alert_pending)에 넣음.

    예전엔 발송 결과를 보지 않고 alert_sent를 올렸고, 약한 후보는 보내기 전에 '알림 완료'로 적어 실패하면 다시 안 갔음.
    """
    now = now or datetime.now(timezone.utc)
    top = matches[0] if matches else {}
    try:
        ok = bool(notifier.notify(row, matches, weak=weak))
    except Exception as e:  # noqa: BLE001 — 메일 만들기(번역 등) 오류도 실패로 보고 재발송
        print(f"  ! {row['id']} 알림 메일 만들기 실패({type(e).__name__})")
        ok = False
    if ok:
        store.bump("alert_sent")
        if top.get("atc_id"):
            store.mark_weak_notified(row["id"], top["atc_id"])  # 알린 후보(유력·약한 공통) — 같은 후보로 반복 알림 방지
        if row.get("alert_pending"):
            store.set_alert_pending(row["id"], None)
        return True
    store.bump("alert_failed")
    tries = int((row.get("alert_pending") or {}).get("tries") or 0)
    if tries >= len(RETRY_MINUTES):
        store.set_alert_pending(row["id"], None)
        store.bump("alert_gaveup")
        try:
            from apps import ops
            ops.alert("알림 메일 재발송 포기", f"신고 {row['id'][:8]}… 알림 메일이 {tries + 1}번 실패했어요. "
                                          "메일 발송 설정(SMTP·Maileroo 한도)을 확인하세요.")
        except Exception:  # noqa: BLE001
            pass
        return False
    nxt = (now + timedelta(minutes=RETRY_MINUTES[tries])).isoformat()
    store.set_alert_pending(row["id"], {"weak": weak, "tries": tries + 1, "next": nxt})
    return False


def retry_pending_alerts(now: datetime | None = None, verbose: bool = True) -> int:
    """재발송 대기 중이고 때가 된 알림을 다시 보냄. 보낸 수 반환. 서버가 15분마다 + 밤 재매칭 시작 때 부름."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for row in store.list_alert_pending():
        p = row.get("alert_pending") or {}
        if p.get("next") and datetime.fromisoformat(p["next"]) > now:
            continue
        if row.get("status") not in ("open", "candidate") or not can_email(row) or not row.get("matches"):
            store.set_alert_pending(row["id"], None)  # 그새 확인·삭제·이메일 해제 → 보낼 이유 없음
            continue
        # 먼저 '처리 중'으로 미뤄 둠 — 서버의 15분 재발송과 밤 재매칭이 같은 신고를 동시에 집어 두 번 보내지 않게
        store.set_alert_pending(row["id"], dict(p, next=(now + timedelta(minutes=10)).isoformat()))
        if send_alert(row, row["matches"], weak=bool(p.get("weak")), now=now):
            sent += 1
    if verbose and sent:
        print(f"알림 메일 재발송 {sent}건")
    return sent


def _rematch_one(row: dict, verbose: bool) -> dict | None:
    seen = row.get("graded_ids")
    matches = graph.match_stored(
        row["text"], row["extracted"] or {}, row["region_set"] or [], row["queries"] or [],
        lang=row.get("lang") or "ko", skip_keys=set(seen) if seen else None,
    )
    if matches is None:  # 상위 후보가 지난번과 같음 → 판정·알림 생략(LLM 비용 0)
        if verbose:
            print(f"  · {row['id']} 새 후보 없음 → 판정 생략")
        return None
    new_keys = graph.graded_keys(matches)
    if new_keys:  # 판정이 실패(Gemini 장애)했으면 기록하지 않음 → 다음 재매칭에서 다시 판정
        store.set_graded(row["id"], list(dict.fromkeys((seen or []) + new_keys)))
    dismissed = set(row.get("dismissed") or [])  # '아니에요' 한 후보 제외(옛 기록은 관리번호, 새 기록은 관리번호_순번)
    matches = [m for m in matches if not is_dismissed(m, dismissed)]
    top = matches[0] if matches else None
    g = top.get("grade") if top else None
    level = alert_level(g, row.get("notify_weak", False), top) if top else None
    notified = set(row.get("weak_notified") or [])
    current = (row.get("best_match") or {}).get("atc_id") if row.get("status") == "candidate" else None
    if level == "strong":
        if top.get("atc_id") == current:  # 이미 알린 유력 후보 그대로 — 후보 목록만 갱신
            store.set_matches(row["id"], slim_matches(matches))
            return None
        store.update_match(row["id"], top, g, "candidate", matches=slim_matches(matches))  # 확정 아닌 '유력 후보'
        if can_email(row) and top.get("atc_id") not in notified:
            send_alert(dict(row, status="candidate"), matches)  # 최고 후보 + 비슷한 후보 목록
        if verbose:
            print(f"  · {row['id']} 새 유력 후보 grade={g}" + (" (기존 유력 후보 있음 — 계속 탐색 중)" if current else ""))
        return {"lost": row, "match": top}
    if (level == "weak" and row.get("status") == "open" and top.get("atc_id") not in notified and can_email(row)):
        store.set_matches(row["id"], slim_matches(matches))
        send_alert(row, matches, weak=True)  # open 유지 — 더 나은 후보 계속 탐색. 알린 후보 기록은 성공했을 때만
        if verbose:
            print(f"  · {row['id']} 약한 후보 grade={g} → 참고 알림(open 유지)")
    elif verbose:
        print(f"  · {row['id']} 최고 grade={g} (임계 미달 또는 이미 알림)")
    return None


def purge_old(now: datetime | None = None, verbose: bool = True) -> int:
    """보유 기간 경과 신고 삭제 — 알림 기간 종료 + retention_days가 지난 신고(상태 무관).

    개인정보처리방침의 보유 기간을 코드로 지킨다. 신고자가 기간을 늘리면 삭제도 그만큼 늦어짐.
    """
    now = now or datetime.now(timezone.utc)
    grace = timedelta(days=settings.retention_days)
    n = 0
    for row in store.all_items():
        if watch_deadline(row) + grace < now:
            store.delete(row["id"])
            n += 1
    if verbose and n:
        print(f"보유 기간 경과 신고 {n}건 삭제")
    return n


def rematch_open(verbose: bool = True) -> list[dict]:
    store.init()
    purge_old(verbose=verbose)
    retry_pending_alerts(verbose=verbose)
    opens = store.list_watching()  # 찾는 중 + 유력 후보(후보가 틀렸을 수 있어 확인·삭제·기간 끝까지 계속)
    if verbose:
        print(f"재매칭 대상(찾는 중·유력 후보): {len(opens)}건 / 임계 grade {settings.rematch_grade_threshold}"
              f" (약한 후보 옵트인 ≥{settings.weak_grade_threshold})")
    hits: list[dict] = []
    failed = 0
    now = datetime.now(timezone.utc)
    for row in opens:
        if watch_deadline(row) < now:  # 알림 기간 만료 → 재매칭(LLM 비용) 중단. 결과창에서 기간 재설정 시 재개.
            store.set_status(row["id"], "expired")
            if verbose:
                print(f"  · {row['id']} 알림 기간 만료 → expired")
            continue
        try:  # 한 건 실패(LLM·검색 오류)가 나머지 신고 재매칭을 막지 않게
            hit = _rematch_one(row, verbose)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ! {row['id']} 재매칭 실패({type(e).__name__}: {e}) → 다음 신고로")
            continue
        if hit:
            hits.append(hit)
    if verbose:
        print(f"신규 매칭 {len(hits)}건" + (f", 실패 {failed}건" if failed else ""))
    return hits


if __name__ == "__main__":
    rematch_open()
