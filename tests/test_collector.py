"""수집기 — 최근 N일 창 재조회 시 이미 색인된 항목 스킵, 새 항목 0건이어도 재매칭 진행, bulk 분할.

외부 서비스 없음: data.go.kr(client)·OpenSearch(urlopen)·재매칭은 목킹.
"""

import io
import json
import sys

from apps.collector import run


def _li(atc: str) -> dict:
    return {"atcId": atc, "fdSn": "1", "fdPrdtNm": "지갑", "prdtClNm": "지갑 > 남성용", "depPlace": "강남경찰서"}


def test_fetch_skips_existing_and_enriches_only_new(monkeypatch):
    pages = {1: [_li("A"), _li("B")], 2: [_li("C")]}
    monkeypatch.setattr(run.client, "get_list", lambda s, a, b, page=1, rows=100:
                        {"items": pages.get(page, []), "total": 3, "code": "00", "msg": ""})
    detailed = []
    monkeypatch.setattr(run.client, "get_detail", lambda s, atc, sn: detailed.append(atc) or {})
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "existing_ids", lambda ids: {"A_1", "C_1"})

    docs = run.fetch("police", "20260901", "20260907", rows=2, max_items=100, enrich=True, skip_existing=True)

    assert [d["atc_id"] for d in docs] == ["B"]
    assert detailed == ["B"]                     # 상세(항목당 1콜)는 새 항목에만


def test_fetch_without_skip_keeps_all(monkeypatch):
    monkeypatch.setattr(run.client, "get_list", lambda *a, **k:
                        {"items": [_li("A"), _li("B")], "total": 2, "code": "00", "msg": ""})
    monkeypatch.setattr(run, "existing_ids", lambda ids: (_ for _ in ()).throw(AssertionError("호출 금지")))
    docs = run.fetch("police", "20260901", "20260901", rows=100, max_items=100, enrich=False)
    assert [d["atc_id"] for d in docs] == ["A", "B"]


def test_main_with_no_new_items_skips_index_but_rematches(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run", "--source", "both", "--start", "20260901",
                                      "--end", "20260907", "--enrich", "--rematch"])
    monkeypatch.setattr(run, "new_items", lambda *a, **k: [])
    monkeypatch.setattr(run, "bulk_index", lambda docs: (_ for _ in ()).throw(AssertionError("빈 bulk 금지")))
    monkeypatch.setattr(run.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b'{"count": 5985}'))
    called = []
    monkeypatch.setattr("apps.agent.rematch.rematch_open", lambda: called.append(1))

    run.main()                                   # 예전엔 빈 _bulk → HTTP 400으로 죽고 재매칭도 건너뜀

    assert called == [1]


def test_bulk_index_splits_into_chunks(monkeypatch):
    sizes = []

    def fake_urlopen(req, timeout=0):
        n = len(req.data.decode().strip().split("\n")) // 2
        sizes.append(n)
        return io.BytesIO(json.dumps({"took": 1, "items": [{"index": {"status": 201}}] * n}).encode())

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    docs = [{"atc_id": str(i), "fd_sn": "1"} for i in range(1200)]
    res = run.bulk_index(docs, chunk=500)
    assert sizes == [500, 500, 200] and res == {"took": 3, "errors": 0, "count": 1200}


def test_main_indexes_in_batches_so_failure_keeps_earlier_work(monkeypatch):
    """배치마다 상세→임베딩→색인 — 중간 실패해도 앞 배치는 이미 색인(예전엔 끝에 한 번에 색인해 전부 유실)."""
    monkeypatch.setattr(sys, "argv", ["run", "--source", "police", "--start", "20260901", "--end", "20260907"])
    monkeypatch.setattr(run, "new_items", lambda *a, **k: [_li(str(i)) for i in range(1200)])
    monkeypatch.setattr(run, "BATCH", 500)
    indexed = []

    def embed_and_index(docs):
        if len(indexed) == 2:
            raise RuntimeError("gemini down")      # 세 번째 배치에서 실패
        indexed.append(len(docs))
        return {"count": len(docs), "errors": 0, "took": 1}
    monkeypatch.setattr(run, "embed_and_index", embed_and_index)
    import pytest
    with pytest.raises(RuntimeError):
        run.main()
    assert indexed == [500, 500]                     # 앞 두 배치(1,000건)는 이미 저장됨


def test_failed_detail_is_not_indexed_so_next_run_retries(monkeypatch):
    """상세가 끝내 실패한 항목은 이번에 색인하지 않음 → 다음 수집이 '새 항목'으로 재시도(예전엔 빈 상세로 영구 색인)."""
    monkeypatch.setattr(run.time, "sleep", lambda s: None)

    def detail(s, atc, sn):
        if atc == "B":
            raise RuntimeError("data.go.kr 요청 실패: Remote end closed connection without response")
        return {"fdPlace": "강남역", "uniq": "내용", "orgNm": "강남경찰서", "tel": "02-000-0000"}

    monkeypatch.setattr(run.client, "get_detail", detail)
    docs = run.enrich_docs("police", [_li("A"), _li("B"), _li("C")], enrich=True)
    assert [d["atc_id"] for d in docs] == ["A", "C"]          # 순서 유지, 실패한 B 제외
    assert docs[0]["found_place"] == "강남역" and docs[0]["org_name"] == "강남경찰서"


def test_client_retries_remote_disconnected(monkeypatch):
    """'Remote end closed connection'(RemoteDisconnected)도 재시도 — 예전엔 못 잡아 바로 실패."""
    import http.client

    from apps.collector import client
    calls = []

    def flaky(url, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return io.BytesIO(b"<response><body><items><item><atcId>A</atcId></item></items></body></response>")

    monkeypatch.setattr(client.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert client.get_detail("police", "A", "1") == {"atcId": "A"}
    assert len(calls) == 3


def test_prune_old_deletes_items_past_holding_period(monkeypatch):
    from datetime import date
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return io.BytesIO(b'{"deleted": 42}')

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    assert run.prune_old(days=185, today=date(2026, 11, 9)) == 42
    assert "_delete_by_query" in seen["url"]
    assert seen["body"] == {"query": {"range": {"found_at": {"lt": "2026-05-08"}}}}


def test_existing_ids_treats_docs_without_detail_as_missing(monkeypatch):
    """습득장소 없이 색인된 문서(예전 상세 실패분)는 '없는 것'으로 → 다음 수집에서 상세 재조회(자가 복구)."""
    docs = {"docs": [{"_id": "A_1", "found": True, "_source": {"found_place": "강남역"}},
                     {"_id": "B_1", "found": True, "_source": {"found_place": ""}},
                     {"_id": "C_1", "found": False}]}
    monkeypatch.setattr(run.urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(json.dumps(docs).encode()))
    assert run.existing_ids(["A_1", "B_1", "C_1"]) == {"A_1"}


# ── 정리: 경찰 목록과 대조(종결 삭제·사진 갱신) ──

def _reconcile_env(monkeypatch, indexed: dict, detail_state: str = "종결"):
    sent = []
    monkeypatch.setattr(run, "indexed_in_range", lambda *a: dict(indexed))
    monkeypatch.setattr(run, "_bulk_actions", lambda lines: sent.extend(lines) or 0)
    monkeypatch.setattr(run.client, "get_detail", lambda *a: {"csteSteNm": detail_state})
    return sent


def test_reconcile_deletes_closed_and_updates_changed_photos(monkeypatch):
    sent = _reconcile_env(monkeypatch, {"A_1": "http://x/a.jpg", "B_1": "http://x/b.jpg", "C_1": ""})
    listed = [dict(_li("A"), fdFilePathImg="http://x/a.jpg"),           # 그대로
              dict(_li("B"), fdFilePathImg="https://p/img02_no_img.gif")]  # 사진 내려감 / C는 목록에 없음(종결)
    stat = run.reconcile("police", "20261104", "20261110", listed, complete=True)
    assert stat["closed"] == 1 and stat["photo"] == 1 and not stat["skipped"]
    acts = [json.loads(line) for x in sent for line in x.split("\n")]
    assert {"delete": {"_index": run.settings.opensearch_index, "_id": "C_1"}} in acts
    assert {"doc": {"image_url": "https://p/img02_no_img.gif"}} in acts
    assert not any(a.get("delete", {}).get("_id") in ("A_1", "B_1") for a in acts)


def test_reconcile_holds_deletion_when_sample_is_still_held(monkeypatch):
    """목록에서 빠졌는데 상세는 '보관중' → 목록 누락 의심 → 삭제 안 함(사진 갱신은 진행)."""
    sent = _reconcile_env(monkeypatch, {"A_1": "", "C_1": ""}, detail_state="보관중")
    stat = run.reconcile("police", "20261104", "20261110", [_li("A")], complete=True)
    assert stat["closed"] == 0 and "보관중" in stat["skipped"]
    assert not any("delete" in json.loads(x.split("\n")[0]) for x in sent)


def test_reconcile_skips_incomplete_or_empty_lists(monkeypatch):
    sent = _reconcile_env(monkeypatch, {"A_1": "", "C_1": ""})
    assert run.reconcile("police", "20261104", "20261110", [_li("A")], complete=False)["skipped"]  # 목록 중간에 끊김
    stat = run.reconcile("police", "20261104", "20261110", [], complete=True)                      # 목록 0건(장애 의심)
    assert stat["closed"] == 0 and stat["skipped"] and sent == []


def test_reconcile_dry_run_changes_nothing(monkeypatch):
    sent = _reconcile_env(monkeypatch, {"C_1": ""})
    stat = run.reconcile("police", "20261104", "20261110", [_li("A")], complete=True, dry_run=True)
    assert stat["closed"] == 1 and sent == []


def test_list_all_reports_whether_the_list_was_complete(monkeypatch):
    pages = {1: [_li("A"), _li("B")], 2: [_li("C")]}
    monkeypatch.setattr(run.client, "get_list", lambda s, a, b, page, rows: {
        "items": pages.get(page, []), "total": 3, "code": "00", "msg": ""})
    items, complete = run.list_all("police", "20261104", "20261110", 2, 100)
    assert len(items) == 3 and complete
    monkeypatch.setattr(run.client, "get_list", lambda s, a, b, page, rows: {
        "items": [] if page > 1 else [_li("A")], "total": 3, "code": "00", "msg": ""})
    assert run.list_all("police", "20261104", "20261110", 1, 100)[1] is False   # 3건 중 1건만 받음


def test_older_range_rotates_through_holding_period():
    from datetime import date, timedelta
    seen = set()
    for k in range(10):
        a, b = run.older_range(date(2026, 11, 12) + timedelta(days=k), keep_days=185, chunk_days=30)
        assert a <= b
        seen.add(a)
    assert len(seen) >= 6                                    # 며칠에 걸쳐 오래된 구간을 고루 돌아감
    a, b = run.older_range(date(2026, 11, 12), keep_days=185, chunk_days=30)
    assert "20260511" <= a and b <= "20261104"              # 보관 기간 안 · 최근 7일 창 밖


def test_bulk_never_splits_an_update_pair(monkeypatch):
    """수정 동작(2줄)이 덩어리 경계에서 갈라지면 OpenSearch가 400 — 11/13 첫 실행 실패 원인. 동작 단위로 나눠야 함."""
    bodies = []

    def fake_urlopen(req, timeout=0):
        bodies.append(req.data.decode())
        return io.BytesIO(b'{"items": []}')

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    acts = [json.dumps({"delete": {"_id": "D%d" % i}}) for i in range(3)]
    acts += [json.dumps({"update": {"_id": "U%d" % i}}) + "\n" + json.dumps({"doc": {"image_url": "u"}}) for i in range(4)]
    assert run._bulk_actions(acts, chunk=2) == 0
    for body in bodies:
        lines = body.strip("\n").split("\n")
        for k, line in enumerate(lines):
            if "update" in json.loads(line):
                assert k + 1 < len(lines) and "doc" in json.loads(lines[k + 1])   # 내용 줄이 같은 덩어리에
    assert sum(b.count('"delete"') for b in bodies) == 3 and sum(b.count('"update"') for b in bodies) == 4


def _catch_up_env(monkeypatch, count: int):
    """수집 창 목록 = W, 오래된 구간 목록 = O1~O3. 색인된 것 없음. 색인 건수는 count."""
    monkeypatch.setattr(run, "list_all", lambda src, a, b, rows, mx: (
        ([_li("W")] if a == "20261106" else [_li("O1"), _li("O2"), _li("O3")]), True))
    monkeypatch.setattr(run, "reconcile", lambda *a, **k: {"source": a[0], "range": "", "listed": 0, "indexed": 0,
                                                             "closed": 0, "photo": 0, "skipped": ""})
    monkeypatch.setattr(run, "existing_ids", lambda ids: set())
    monkeypatch.setattr(run, "_os", lambda *a, **k: {"count": count})
    monkeypatch.setattr(run, "enrich_docs", lambda src, items, enrich: [
        {"atc_id": li["atcId"], "name": "지갑", "color": "", "category": "", "dep_place": "", "found_place": "",
         "description": ""} for li in items])
    got = []
    monkeypatch.setattr(run, "embed_and_index", lambda docs: got.extend(d["atc_id"] for d in docs)
                        or {"count": len(docs), "errors": 0})
    monkeypatch.setattr(run, "prune_old", lambda: 0)
    monkeypatch.setattr(run.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b'{"count": 1}'))
    monkeypatch.setattr(sys, "argv", ["run", "--source", "police", "--start", "20261106", "--end", "20261112",
                                      "--enrich", "--reconcile", "--catch-up", "2"])
    return got


def test_catch_up_collects_late_registrations_up_to_the_nightly_cap(monkeypatch):
    got = _catch_up_env(monkeypatch, count=181_874)
    run.main()
    assert got == ["W", "O1", "O2"]          # 수집 창 새 항목 + 오래된 구간에서 빠진 것 2건(상한)


def test_catch_up_stops_when_index_is_large(monkeypatch):
    got = _catch_up_env(monkeypatch, count=600_000)
    run.main()
    assert got == ["W"]                      # 색인 상한(기본 60만)이면 늦은 등록 수집 안 함 — 서버 메모리 보호
