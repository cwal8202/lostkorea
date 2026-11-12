"""수집기 CLI — data.go.kr 목록 페이징 → (새 항목만) 상세 enrich → 임베딩 → OpenSearch 색인(upsert).

스케줄과 무관한 멱등 명령. 언제 돌릴지는 외부 스케줄러(cron/작업 스케줄러/클라우드)가 결정.
문서 _id = atcId_fdSn 라 재실행은 upsert(중복 없음).

습득물은 습득일 이후 며칠에 걸쳐 등록됨(실측: 자정에 '어제분' 조회 시 5건 → 하루 뒤 ~2,800건).
그래서 매일 **최근 N일 창을 다시 조회**하고, 이미 색인된 항목은 건너뛰어 상세 조회·임베딩 비용은 새 항목에만 씀.

예:
  # 검증(임베딩·색인 없이 몇 건만 확인)
  uv run python -m apps.collector.run --source police --start 20260831 --end 20260831 --max 5 --dry-run
  # 실제 색인(상세 enrich + 임베딩, 이미 색인된 건 스킵)
  uv run python -m apps.collector.run --source both --start 20260901 --end 20260907 --max 20000 --enrich
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from apps.api import gemini
from apps.api.config import settings
from apps.api.region import resolver as region_resolver
from apps.collector import client
from apps.collector.normalize import doc_id, embed_text, list_id, normalize


def list_items(source: str, start: str, end: str, rows: int, max_items: int) -> list[dict]:
    """목록만 페이징(상세 X) — 가벼움. 최대 max_items건."""
    items: list[dict] = []
    page = 1
    while len(items) < max_items:
        res = client.get_list(source, start, end, page=page, rows=min(rows, max_items - len(items)))
        if res["code"] not in ("00", ""):
            print(f"  ⚠ {source} 목록 응답: {res['code']} {res['msg']}")
            break
        if not res["items"]:
            break
        items += res["items"]
        print(f"  {source} p{page}: 목록 {len(items)}/{min(max_items, res['total'])} (total {res['total']})")
        if len(items) >= res["total"]:
            break
        page += 1
    return items[:max_items]


def list_all(source: str, start: str, end: str, rows: int, max_items: int) -> tuple[list[dict], bool]:
    """목록 전부 + '끝까지 받았는지'. 대조(정리)는 끝까지 받은 구간에서만 — 중간에 끊긴 목록으로 지우면 안 됨."""
    items: list[dict] = []
    page, total = 1, -1
    while len(items) < max_items:
        res = client.get_list(source, start, end, page=page, rows=rows)
        if res["code"] not in ("00", ""):
            print(f"  ⚠ {source} 목록 응답: {res['code']} {res['msg']}")
            return items, False
        total = res["total"]
        if not res["items"]:
            break
        items += res["items"]
        if len(items) >= total:
            break
        page += 1
    return items[:max_items], total >= 0 and len(items) == total


def existing_ids(ids: list[str], chunk: int = 1000) -> set[str]:
    """이미 (상세까지) 색인된 문서 id 집합(OpenSearch _mget, found_place만 조회).

    습득장소가 빈 문서는 예전에 상세 조회가 실패한 채 색인된 것 → '없는 것'으로 보고 다시 상세 조회(자가 복구).
    """
    found: set[str] = set()
    for i in range(0, len(ids), chunk):
        body = json.dumps({"ids": ids[i : i + chunk]}).encode()
        req = urllib.request.Request(
            f"{settings.opensearch_url}/{settings.opensearch_index}/_mget?_source=found_place",
            body, {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            found |= {d["_id"] for d in json.load(r).get("docs", [])
                      if d.get("found") and (d.get("_source") or {}).get("found_place")}
    return found


def _detail(source: str, li: dict) -> dict | None:
    """상세 1건. 재시도 후에도 실패하면 None."""
    try:
        return client.get_detail(source, li["atcId"], li["fdSn"])
    except Exception as e:  # noqa: BLE001
        print(f"  ! 상세 실패 {li.get('atcId')}: {e}")
        return None
    finally:
        time.sleep(0.1)  # 상세는 항목당 1콜 — 예의상 간격(작업자마다)


def enrich_docs(source: str, items: list[dict], enrich: bool) -> list[dict]:
    """목록 항목 → (선택) 상세 조회 → 정규화 문서. 상세는 항목당 1콜이라 새 항목에만 호출.

    - 상세는 collect_workers개 동시 요청(건당 ~1초라 순차로는 하루 3~6천 건에 1~8시간).
    - 상세가 끝내 실패한 항목은 **이번에 색인하지 않음** → 다음 수집(최근 7일 창 재조회)이 '새 항목'으로 다시 시도.
      예전엔 습득장소·특이사항·보관기관 없이 색인돼 다음부터 '이미 색인'으로 건너뛰어 영구히 빈 채로 남았다.
    """
    if not enrich:
        return [normalize(source, li, {}) for li in items]
    with ThreadPoolExecutor(max_workers=max(1, settings.collect_workers)) as ex:
        details = list(ex.map(lambda li: _detail(source, li), items))
    docs = [normalize(source, li, d) for li, d in zip(items, details) if d is not None]
    failed = len(items) - len(docs)
    print(f"  {source} 상세 {len(items)}건" + (f" (실패 {failed}건은 다음 수집에서 재시도)" if failed else ""),
          flush=True)
    return docs


def prune_old(days: int | None = None, today: date | None = None) -> int:
    """습득일이 days일보다 오래된 습득물 삭제 — 유실물법상 소유자가 찾아갈 수 있는 기간(6개월)이 지난 것.

    안 지우면 색인이 하루 ~3,700건씩 끝없이 늘어 벡터 검색 메모리·디스크가 서버를 넘어선다(10월 기준 17만 건·3GB).
    """
    days = days or settings.found_retention_days
    cutoff = ((today or date.today()) - timedelta(days=days)).isoformat()
    body = json.dumps({"query": {"range": {"found_at": {"lt": cutoff}}}}).encode()
    req = urllib.request.Request(
        f"{settings.opensearch_url}/{settings.opensearch_index}/_delete_by_query?conflicts=proceed",
        body, {"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        deleted = json.load(r).get("deleted", 0)
    if deleted:
        print(f"  보관 기간 경과(습득일 < {cutoff}) {deleted}건 삭제")
    return deleted


# ── 정리: 경찰 목록과 대조해 종결된 습득물 삭제 + 바뀐 사진 반영 ──
# 경찰 목록 API는 '보관중'만 돌려줌(종결 = 주인에게 반환 등 처리 끝난 물건은 빠짐 — 11/12 확인).
# 그런데 수집은 새 항목만 넣고 다시 보지 않아, 종결된 물건과 내려간 사진이 계속 남았음
# (11/12 실측: 최근 7일 경찰서 색인의 27%, 9~10월 습득분의 30~45%가 이미 종결).
# 안전장치: 목록을 끝까지 받은 구간만 / 목록이 비면 건너뜀 / 지울 대상 일부를 상세 조회해 '보관중'이 하나라도 있으면 삭제 보류.
CLOSED_SAMPLE = 5


def _ymd_iso(ymd: str) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _os(path: str, body: dict | None = None, method: str = "POST", timeout: int = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{settings.opensearch_url}{path}", data, {"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def indexed_in_range(source: str, start: str, end: str) -> dict[str, str]:
    """색인된 문서 {_id: image_url} — 출처·습득일 구간(YYYYMMDD) 전부(scroll)."""
    body = {"size": 5000, "_source": ["image_url"], "sort": ["_doc"], "query": {"bool": {"filter": [
        {"term": {"source": source}}, {"range": {"found_at": {"gte": _ymd_iso(start), "lte": _ymd_iso(end)}}}]}}}
    res = _os(f"/{settings.opensearch_index}/_search?scroll=2m", body)
    sid, out = res.get("_scroll_id"), {}
    try:
        while res["hits"]["hits"]:
            out.update({h["_id"]: (h.get("_source") or {}).get("image_url", "") for h in res["hits"]["hits"]})
            res = _os("/_search/scroll", {"scroll": "2m", "scroll_id": sid})
            sid = res.get("_scroll_id", sid)
    finally:
        if sid:
            try:
                _os("/_search/scroll", {"scroll_id": sid}, method="DELETE")
            except Exception:  # noqa: BLE001 — 정리 못 해도 2분 뒤 자동 만료
                pass
    return out


def _bulk_actions(actions: list[str], chunk: int = 1000) -> int:
    """삭제·부분 수정 _bulk. 오류 건수 반환(이미 없는 문서 삭제 404는 오류 아님).

    actions의 한 항목 = 동작 하나(삭제는 1줄, 수정은 '동작 줄\n내용 줄' 2줄) — **동작 단위로** 나눠 보냄.
    11/13 첫 실행: 줄 단위로 2,000줄씩 자르다 수정 동작의 두 줄이 갈라져 400(Bad Request) → 앞 덩어리만 반영되고 멈췄음.
    """
    errors = 0
    for i in range(0, len(actions), chunk):
        body = ("\n".join(actions[i : i + chunk]) + "\n").encode("utf-8")
        req = urllib.request.Request(f"{settings.opensearch_url}/_bulk", body, {"Content-Type": "application/x-ndjson"})
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.load(r)
        errors += sum(1 for it in res.get("items", []) for v in it.values()
                      if v.get("status", 200) >= 300 and v.get("status") != 404)
    return errors


def _closed_confirmed(source: str, ids: list[str]) -> bool:
    """지울 대상 표본을 상세 조회 — 하나라도 '보관중'이면 False(목록 누락 의심). 확인된 게 하나도 없어도 False."""
    confirmed = 0
    for i in ids:
        atc, _, sn = i.rpartition("_")
        try:
            d = client.get_detail(source, atc, sn or "1")
        except Exception:  # noqa: BLE001
            continue
        if (d.get("csteSteNm") or "").strip() == "보관중":
            return False
        confirmed += 1
    return confirmed > 0


def reconcile(source: str, start: str, end: str, listed: list[dict], complete: bool, dry_run: bool = False) -> dict:
    """색인(출처·습득일 구간)을 경찰 목록과 대조 → 목록에 없는 것(종결) 삭제, 사진 주소가 바뀐 것 갱신."""
    stat = {"source": source, "range": f"{start}~{end}", "listed": len(listed), "indexed": 0,
            "closed": 0, "photo": 0, "skipped": ""}
    if not complete:
        stat["skipped"] = "목록을 끝까지 받지 못함"
        return stat
    have = indexed_in_range(source, start, end)
    stat["indexed"] = len(have)
    now = {list_id(li): li.get("fdFilePathImg", "") for li in listed}
    gone = [i for i in have if i not in now]
    if gone and not listed:
        stat["skipped"] = "목록이 비었음(장애 의심) — 삭제 보류"
        gone = []
    elif gone and not _closed_confirmed(source, random.sample(gone, min(CLOSED_SAMPLE, len(gone)))):
        stat["skipped"] = "표본 중 보관중이 있음(목록 누락 의심) — 삭제 보류"
        gone = []
    changed = {i: now[i] for i in have if i in now and now[i] and now[i] != have[i]}
    stat["closed"], stat["photo"] = len(gone), len(changed)
    if not dry_run and (gone or changed):
        idx = settings.opensearch_index
        actions = [json.dumps({"delete": {"_index": idx, "_id": i}}) for i in gone]
        actions += [json.dumps({"update": {"_index": idx, "_id": i}}) + "\n" + json.dumps({"doc": {"image_url": url}})
                    for i, url in changed.items()]
        stat["errors"] = _bulk_actions(actions)
    return stat


def older_range(today: date, keep_days: int, chunk_days: int = 30, recent_days: int = 7) -> tuple[str, str]:
    """수집 창(최근 recent_days일)보다 오래된 보관 기간(keep_days일)을 chunk_days일씩 돌아가며 — 밤마다 한 덩어리.

    보관 기간 185일·덩어리 30일이면 7일마다 전체를 한 번씩 대조(목록 호출은 하루 수십 건 — 한도 10만/일).
    """
    oldest = today - timedelta(days=keep_days)
    newest = today - timedelta(days=recent_days + 1)
    n = max(1, -(-((newest - oldest).days + 1) // chunk_days))
    a = oldest + timedelta(days=(today.toordinal() % n) * chunk_days)
    b = min(newest, a + timedelta(days=chunk_days - 1))
    return a.strftime("%Y%m%d"), b.strftime("%Y%m%d")


def _print_reconcile(stat: dict, dry_run: bool) -> None:
    head = "[정리·시험]" if dry_run else "[정리]"
    print(f"  {head} {stat['source']} {stat['range']}: 경찰 목록 {stat['listed']}건 · 색인 {stat['indexed']}건 → "
          f"종결 삭제 {stat['closed']}건 · 사진 갱신 {stat['photo']}건"
          + (f" · 오류 {stat['errors']}건" if stat.get("errors") else "")
          + (f" ({stat['skipped']})" if stat["skipped"] else ""), flush=True)


BATCH = 500  # 상세 조회 → 임베딩 → 색인을 이 단위로 — 중간에 끊겨도 앞 배치는 남고, 재실행 시 스킵으로 이어감


def new_items(source: str, start: str, end: str, rows: int, max_items: int,
              skip_existing: bool = False, listed: list[dict] | None = None) -> list[dict]:
    """목록 조회 + (선택) 이미 색인된 항목 제외. listed를 주면 목록을 다시 받지 않음(정리에서 이미 받은 것)."""
    items = list_items(source, start, end, rows, max_items) if listed is None else listed
    if skip_existing and items:
        have = existing_ids([list_id(li) for li in items])
        new = [li for li in items if list_id(li) not in have]
        print(f"  {source}: 목록 {len(items)}건 중 이미 색인 {len(items) - len(new)}건 → 새 항목 {len(new)}건만 처리")
        items = new
    return items


def fetch(source: str, start: str, end: str, rows: int, max_items: int, enrich: bool,
          skip_existing: bool = False) -> list[dict]:
    return enrich_docs(source, new_items(source, start, end, rows, max_items, skip_existing), enrich)


def embed_and_index(docs: list[dict]) -> dict:
    """한 배치 임베딩 → _bulk 색인."""
    vecs = gemini.embed_texts([embed_text(d) for d in docs])
    for d, v in zip(docs, vecs):
        d["embedding"] = v
    return bulk_index(docs)


def bulk_index(docs: list[dict], chunk: int = 500) -> dict:
    """_bulk upsert를 chunk건씩 — 벡터 포함 문서 수만 건을 한 요청에 담으면 100MB 한도 초과."""
    took = errors = 0
    for i in range(0, len(docs), chunk):
        lines = []
        for d in docs[i : i + chunk]:
            lines.append(json.dumps({"index": {"_index": settings.opensearch_index, "_id": doc_id(d)}}))
            lines.append(json.dumps(d, ensure_ascii=False))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        req = urllib.request.Request(
            f"{settings.opensearch_url}/_bulk", body, {"Content-Type": "application/x-ndjson"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
        took += res.get("took") or 0
        errors += sum(1 for it in res.get("items", []) if it.get("index", {}).get("status", 200) >= 300)
    return {"took": took, "errors": errors, "count": len(docs)}


def main() -> None:
    ap = argparse.ArgumentParser(description="LostKorea 습득물 수집기")
    ap.add_argument("--source", choices=["police", "portal", "both"], default="both")
    ap.add_argument("--start", required=True, help="START_YMD (YYYYMMDD)")
    ap.add_argument("--end", required=True, help="END_YMD (YYYYMMDD)")
    ap.add_argument("--rows", type=int, default=100, help="페이지당 목록 건수")
    ap.add_argument("--max", type=int, default=100, help="소스별 최대 수집 건수(비용/한도 보호)")
    ap.add_argument("--enrich", action="store_true", help="상세 호출로 습득장소·특이사항 채움(항목당 1콜)")
    ap.add_argument("--no-embed", action="store_true", help="임베딩·색인 생략(정규화까지만)")
    ap.add_argument("--dry-run", action="store_true", help="색인 안 함, 샘플만 출력")
    ap.add_argument("--rematch", action="store_true", help="색인 후 open 분실물 지속 재매칭 실행")
    ap.add_argument("--refresh-existing", action="store_true",
                    help="이미 색인된 항목도 다시 상세 조회·임베딩(기본: 새 항목만)")
    ap.add_argument("--reconcile", action="store_true",
                    help="경찰 목록과 대조해 종결된 습득물 삭제·사진 갱신(수집 창 + 오래된 구간 한 덩어리)")
    ap.add_argument("--reconcile-only", action="store_true", help="대조만(새 항목 수집·재매칭 없이)")
    ap.add_argument("--reconcile-chunk-days", type=int, default=30, help="밤마다 대조할 오래된 구간 길이(일)")
    ap.add_argument("--catch-up", type=int, default=0,
                    help="대조한 오래된 구간에서 늦게 등록돼 빠진 습득물을 출처당 최대 N건 수집(0=끔)")
    ap.add_argument("--catch-up-max-docs", type=int, default=600_000,
                    help="색인이 이 건수 이상이면 늦은 등록 수집을 건너뜀(서버 메모리 보호)")
    args = ap.parse_args()
    args.reconcile = args.reconcile or args.reconcile_only

    indexing = not (args.dry_run or args.no_embed)
    sources = ["police", "portal"] if args.source == "both" else [args.source]
    total = indexed = errors = 0
    sample = None

    def collect(src: str, items: list[dict]) -> None:
        """배치마다 상세 → (임베딩·색인) — 끊겨도 앞 배치는 보존."""
        nonlocal total, indexed, errors, sample
        for i in range(0, len(items), BATCH):
            docs = enrich_docs(src, items[i : i + BATCH], args.enrich)
            for d in docs:  # 구/시 해석해 색인에 저장(둘러보기 지역 필터·집계용, 비용 0)
                d["region"] = region_resolver.resolve(d.get("dep_place", ""))
            total += len(docs)
            sample = sample or (docs[0] if docs else None)
            if indexing and docs:
                res = embed_and_index(docs)
                indexed += res["count"]
                errors += res["errors"]
                print(f"  {src} 배치 {i // BATCH + 1}: {res['count']}건 색인 (누적 {indexed})", flush=True)

    for src in sources:
        print(f"[{src}] 수집 {args.start}~{args.end} (max {args.max}, enrich={args.enrich})")
        listed = o_listed = None
        o_complete = False
        if args.reconcile:
            listed, complete = list_all(src, args.start, args.end, args.rows, args.max)
            try:
                _print_reconcile(reconcile(src, args.start, args.end, listed, complete, dry_run=not indexing),
                                 not indexing)
                o_start, o_end = older_range(date.today(), settings.found_retention_days, args.reconcile_chunk_days)
                o_listed, o_complete = list_all(src, o_start, o_end, args.rows, 500_000)
                _print_reconcile(reconcile(src, o_start, o_end, o_listed, o_complete, dry_run=not indexing),
                                 not indexing)
            except Exception as e:  # noqa: BLE001 — 정리 실패가 수집·재매칭을 막지 않게
                print(f"  ! 정리 실패: {type(e).__name__}: {e}")
            if args.reconcile_only:
                continue
        items = new_items(src, args.start, args.end, args.rows, args.max,
                          skip_existing=indexing and not args.refresh_existing, listed=listed)
        collect(src, items)

        # 늦게 등록된 습득물: 수집 창(7일)이 지난 뒤 등록돼 영영 빠지던 것 — 오늘 대조한 오래된 구간에서 채움.
        # 11/12 실측: 10/20 습득분은 보관 중 2,195건 중 1,293건이 색인에 없었음(부산유실물센터 등 한꺼번에 늦게 등록).
        # 11/13 측정: 50만 건이어도 768차원 벡터 ~1.6GB(서버 여유 ~4GB) → 출처당 하룻밤 상한 + 색인 상한으로 천천히.
        if args.catch_up and indexing and o_listed and o_complete:
            try:
                count = _os(f"/{settings.opensearch_index}/_count", method="GET").get("count", 0)
            except Exception as e:  # noqa: BLE001
                count = None
                print(f"  ! 색인 건수 확인 실패({type(e).__name__}) → 늦은 등록 수집 건너뜀")
            if count is not None and count >= args.catch_up_max_docs:
                print(f"  늦은 등록 수집 건너뜀: 색인 {count}건 ≥ 상한 {args.catch_up_max_docs}건")
            elif count is not None:
                late = new_items(src, o_start, o_end, args.rows, len(o_listed), skip_existing=True, listed=o_listed)
                print(f"  [늦은 등록] {src} {o_start}~{o_end}: 빠진 {len(late)}건 중 {min(len(late), args.catch_up)}건 수집",
                      flush=True)
                collect(src, late[: args.catch_up])

    print(f"\n총 정규화 {total}건")
    if sample:
        d = sample
        print(f"  샘플: {d['name']} / {d['color']} / {d['category']} / 보관:{d['dep_place']} / "
              f"습득장소:{d['found_place'] or '-'} / 특이:{(d['description'] or '-')[:30]}")

    if not indexing:
        print("(dry-run/no-embed — 임베딩·색인 생략)")
        return

    if indexed:  # 새 항목 0건이면 색인 생략(빈 _bulk는 OpenSearch 400) — 재매칭은 그대로 진행
        print(f"  완료: {indexed}건 색인, 오류 {errors}건")
    else:
        print("새 항목 없음 — 색인 생략")
    try:
        prune_old()
    except Exception as e:  # noqa: BLE001 — 정리 실패가 색인·재매칭을 막지 않게
        print(f"  ! 오래된 습득물 정리 실패: {e}")
    urllib.request.urlopen(urllib.request.Request(  # 색인·삭제 즉시 반영
        f"{settings.opensearch_url}/{settings.opensearch_index}/_refresh", method="POST"), timeout=15)
    cnt = json.load(urllib.request.urlopen(
        f"{settings.opensearch_url}/{settings.opensearch_index}/_count", timeout=15))["count"]
    print(f"  현재 인덱스 총 문서: {cnt}")

    if args.rematch:  # 새 습득물 유입 → 저장된 미해결 분실물 재매칭
        print("\n지속 재매칭...")
        from apps.agent.rematch import rematch_open
        rematch_open()


if __name__ == "__main__":
    main()
