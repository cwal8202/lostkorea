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
