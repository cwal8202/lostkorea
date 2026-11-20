"""API 배선·보안 — 헬스체크, 신고 관리 토큰, 공개 목록 제거, 클라이언트 데이터 불신, 레이트리밋·일일 상한,
입력 상한(사진·검색어), 외국어 검색어 번역 캐시. 외부 서비스(OpenSearch·Gemini)는 목킹."""

import base64

import pytest
from fastapi.testclient import TestClient

from apps import store
from apps.api import gemini, main
from apps.api import search as search_mod
from apps.api.config import settings
from apps.api.limits import client_ip
from apps.api.main import app
from apps.api.models import MAX_IMAGE_B64

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    store.init()
    store._run("DELETE FROM lost_items")
    yield


def _report(text="검정 지갑", email="", matches=None):
    """관리 토큰이 있는 신고 하나 → (id, token)."""
    token = store.new_token()
    lid = store.add(text, {}, [], [], None, None, email=email, token_hash=store.hash_token(token),
                    matches=matches or [])
    return lid, token


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200 and "LostKorea" in r.text
    assert "no-store" in r.headers.get("cache-control", "")  # 배포 즉시 반영


def test_ids_are_unguessable():
    lid, _ = _report()
    assert len(lid) == 32                                    # 128비트(기존 48비트 12자리에서 확대)


def test_public_list_removed():
    _report(email="secret@example.com")
    assert client.get("/lost-items").status_code in (404, 405)  # 남의 신고 문장·이메일 공개 목록 제거


def test_mine_returns_only_owned_reports_without_private_fields():
    a, ta = _report("내 지갑", email="secret@example.com")
    b, _ = _report("남의 지갑")
    body = client.post("/lost-items/mine", json={"items": [{"id": a, "token": ta}, {"id": b, "token": "wrong"}]}).json()
    assert [x["text"] for x in body] == ["내 지갑"]
    assert "secret@example.com" not in str(body) and "token_hash" not in str(body)


def test_detail_requires_token():
    lid, token = _report(email="secret@example.com")
    assert client.get(f"/lost-items/{lid}").status_code == 404                       # 토큰 없음
    assert client.get(f"/lost-items/{lid}", headers={"X-Report-Token": "x"}).status_code == 404
    body = client.get(f"/lost-items/{lid}", headers={"X-Report-Token": token}).json()
    assert body["id"] == lid and "email" not in body and "token_hash" not in body


def test_confirm_uses_server_side_match_only():
    lid, token = _report(matches=[{"atc_id": "A_1", "name": "검정 지갑", "grade": 88}])
    h = {"X-Report-Token": token}
    assert client.post(f"/lost-items/{lid}/confirm", json={"atc_id": "EVIL"}, headers=h).status_code == 400
    assert client.post(f"/lost-items/{lid}/confirm", json={"atc_id": "A_1"}).status_code == 404  # 토큰 없음
    assert client.post(f"/lost-items/{lid}/confirm", json={"atc_id": "A_1"}, headers=h).status_code == 200
    row = store.get(lid)
    assert row["status"] == "confirmed" and row["best_match"]["name"] == "검정 지갑"  # 서버 저장 후보


def test_dismiss_requires_token():
    lid, token = _report()
    assert client.post(f"/lost-items/{lid}/dismiss", json={"atc_id": "A_1"}).status_code == 404
    assert client.post(f"/lost-items/{lid}/dismiss", json={"atc_id": "A_1"},
                       headers={"X-Report-Token": token}).json()["dismissed"] == ["A_1"]


def test_search_rate_limited(monkeypatch):
    monkeypatch.setattr(settings, "limit_search_hour", 2)
    monkeypatch.setattr(search_mod, "search", lambda *a, **k: [])
    codes = [client.get("/found-items/search", params={"q": "지갑"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_daily_ai_budget_caps_everyone(monkeypatch):
    monkeypatch.setattr(settings, "daily_ai_budget", 3)
    monkeypatch.setattr(search_mod, "search", lambda *a, **k: [])
    assert client.get("/found-items/search", params={"q": "지갑", "grade": True}).status_code == 200  # 2단위
    r = client.get("/found-items/search", params={"q": "지갑", "grade": True})
    assert r.status_code == 429 and "이용량" in r.json()["detail"]


def test_report_rate_limited_before_any_llm(monkeypatch):
    monkeypatch.setattr(settings, "limit_report_10min", 1)
    called = []
    from apps.agent import graph
    monkeypatch.setattr(graph, "stream", lambda *a, **k: called.append(1) or iter([("done", {"extracted": {"is_lost_report": False}})]))
    assert client.post("/lost-items/stream", json={"text": "검정 지갑"}).status_code == 200
    assert client.post("/lost-items/stream", json={"text": "검정 지갑"}).status_code == 429
    assert called == [1]                                     # 두 번째는 LLM 호출 전에 차단


def test_image_size_and_type_limits():
    big = "A" * (MAX_IMAGE_B64 + 1)
    assert client.post("/lost-items/image/stream", json={"image_b64": big}).status_code == 422
    small = base64.b64encode(b"x").decode()
    assert client.post("/lost-items/image/stream", json={"image_b64": small, "mime": "text/html"}).status_code == 422


def test_browse_limits(monkeypatch):
    assert client.get("/found-items/browse", params={"q": "가" * 101}).status_code == 422
    assert client.get("/found-items/browse", params={"page": 500, "size": 24}).status_code == 400


def test_browse_translation_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini, "generate_json", lambda p, **k: calls.append(p) or {"ko": "지갑"})
    monkeypatch.setattr(search_mod, "_os_raw", lambda body: {"hits": {"hits": [], "total": {"value": 0}}})
    for _ in range(3):
        assert client.get("/found-items/browse", params={"q": "Wallet"}).status_code == 200
    assert len(calls) == 1                                   # 같은 외국어 검색어는 번역 1회
    assert '"q": "Wallet"' in calls[0]                        # 검색어는 JSON 데이터로 감싸 전달


def test_browse_photo_filter_and_sort(monkeypatch):
    bodies = []
    monkeypatch.setattr(search_mod, "_os_raw",
                        lambda body: bodies.append(body) or {"hits": {"hits": [], "total": {"value": 0}}})
    assert client.get("/found-items/browse").status_code == 200
    assert client.get("/found-items/browse", params={"photo": "with", "sort": "old"}).status_code == 200
    assert client.get("/found-items/browse", params={"photo": "without"}).status_code == 200
    plain, with_old, without = (b["query"]["bool"] for b in bodies)
    order = [b["sort"][0]["found_at"]["order"] for b in bodies]
    assert order == ["desc", "asc", "desc"]                          # 기본 최신순, sort=old면 오래된순
    assert not any("script" in f for f in plain["filter"]) and not plain["must_not"]  # 기본은 사진 조건 없음
    assert any("script" in f for f in with_old["filter"])            # 사진 있는 것만
    assert any("script" in f for f in without["must_not"])           # 사진 없는 것만
    assert "no_img" in search_mod._PHOTO_SCRIPT["script"]["source"]  # 경찰청 '사진 없음' 그림은 사진 아님
    assert client.get("/found-items/browse", params={"photo": "all"}).status_code == 422
    assert client.get("/found-items/browse", params={"sort": "random"}).status_code == 422


def test_browse_item_carries_found_place(monkeypatch):
    src = {"atc_id": "F1", "fd_sn": "1", "name": "지갑", "found_place": "택시", "region": "서울특별시 마포구"}
    monkeypatch.setattr(search_mod, "_os_raw", lambda body: {"hits": {"hits": [{"_id": "F1_1", "_source": src}],
                                                                      "total": {"value": 1}}})
    item = client.get("/found-items/browse").json()["items"][0]
    assert item["found_place"] == "택시" and item["atc_id"] == "F1" and item["fd_sn"] == "1"  # 원본 글 링크·습득 장소용


class _Req:
    def __init__(self, host, xff="", cf=""):
        self.client = type("C", (), {"host": host})()
        self.headers = {"x-forwarded-for": xff} if xff else {}
        if cf:
            self.headers["cf-connecting-ip"] = cf


def test_client_ip_trusts_forwarded_only_from_internal_proxy():
    assert client_ip(_Req("172.18.0.5", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"  # 프록시가 붙인 맨 오른쪽
    assert client_ip(_Req("8.8.8.8", "1.2.3.4")) == "8.8.8.8"              # 외부에서 위조한 헤더 무시


def test_client_ip_behind_cloudflare_uses_real_visitor():
    """Cloudflare 프록시를 켜면 Caddy에 접속하는 건 Cloudflare 서버 — 그때만 CF-Connecting-IP(실제 방문자)를 믿는다.
    안 그러면 같은 Cloudflare 서버를 거친 모든 방문자가 한 사람으로 묶여 서로의 요청 한도를 나눠 쓴다."""
    caddy = "172.18.0.5"
    assert client_ip(_Req(caddy, "162.158.10.20", cf="5.6.7.8")) == "5.6.7.8"          # Cloudflare IPv4 경유
    assert client_ip(_Req(caddy, "2400:cb00:2049::1", cf="2001:db8::7")) == "2001:db8::7"  # IPv6 경유
    assert client_ip(_Req(caddy, "1.2.3.4", cf="5.6.7.8")) == "1.2.3.4"    # 직접 접속자가 헤더 위조 → 무시
    assert client_ip(_Req(caddy, "162.158.10.20", cf="not-an-ip")) == "162.158.10.20"  # 이상한 값이면 접속 IP
    assert client_ip(_Req(caddy, "162.158.10.20")) == "162.158.10.20"
    assert client_ip(_Req("8.8.8.8", "162.158.10.20", cf="5.6.7.8")) == "8.8.8.8"     # 내부 프록시가 아니면 그대로


def test_valid_email():
    assert main._valid_email("me@example.com")
    assert not main._valid_email("me@") and not main._valid_email("a b@c.com") and not main._valid_email("")


# ── 습득물 사진 작은 미리보기(/thumb.jpg) ──
_POLICE_IMG = "https://minwon24.police.go.kr/lost112/find/getOpenapiAttachFileImage/F1/1/C1/1.do"


def _jpeg(w=1200, h=800) -> bytes:
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (w, h), (30, 60, 90)).save(buf, "JPEG")
    return buf.getvalue()


def test_thumb_only_for_police_photos():
    for u in ("https://evil.example/x.jpg", "http://minwon24.police.go.kr/lost112/find/getOpenapiAttachFileImage/F1/1/C1/1.do",
              "https://minwon24.police.go.kr/images/sub/img02_no_img.gif", "https://minwon24.police.go.kr/main.do"):
        assert client.get("/thumb.jpg", params={"u": u}, follow_redirects=False).status_code == 400  # 아무 주소나 대신 받지 않음


def test_thumb_resizes_and_caches(monkeypatch, tmp_path):
    import io as _io

    from PIL import Image

    from apps.api import thumbs
    calls = []
    monkeypatch.setattr(thumbs, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(thumbs, "_fetch", lambda u: calls.append(1) or _jpeg())
    r = client.get("/thumb.jpg", params={"u": _POLICE_IMG})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "public" in r.headers["cache-control"] and "max-age" in r.headers["cache-control"]   # Cloudflare가 저장
    assert Image.open(_io.BytesIO(r.content)).size == (256, 256) and len(r.content) < 30_000
    client.get("/thumb.jpg", params={"u": _POLICE_IMG})
    assert calls == [1]                                            # 두 번째는 서버 캐시


def test_thumb_falls_back_to_original(monkeypatch, tmp_path):
    from apps.api import thumbs
    monkeypatch.setattr(thumbs, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(thumbs, "_fetch", lambda u: (_ for _ in ()).throw(OSError("timeout")))
    r = client.get("/thumb.jpg", params={"u": _POLICE_IMG}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == _POLICE_IMG                       # 사진이 사라지지 않게


def test_thumb_prefill_newest_first_within_cap(monkeypatch, tmp_path):
    """밤 수집이 미리 만들기 — 없는 것만, 앞(최신)부터 max_new장까지, 실패는 건너뜀."""
    import io as _io

    from apps.api import thumbs
    urls = [_POLICE_IMG.replace("/1.do", f"/{i}.do") for i in range(5)]
    monkeypatch.setattr(thumbs, "CACHE_DIR", tmp_path)
    (tmp_path / thumbs._name(urls[0])).write_bytes(b"x")          # 이미 있는 사진은 다시 받지 않음
    got = []

    def fake(u):
        got.append(u)
        if u == urls[2]:
            raise OSError("timeout")
        return _jpeg()
    monkeypatch.setattr(thumbs, "_fetch", fake)
    st = thumbs.prefill(urls + ["https://evil.example/x.jpg"], max_new=3, budget_s=60, workers=2)
    assert sorted(got) == sorted(urls[1:4])                         # 최신순 앞 3장(이미 있는 0번 제외), 남의 주소 제외
    assert st == {"missing": 4, "made": 2, "failed": 1, "left": 2}
    assert (tmp_path / thumbs._name(urls[1])).exists() and not (tmp_path / thumbs._name(urls[4])).exists()


def test_thumb_fetch_reuses_pool_and_retries_only_dropped(monkeypatch):
    """경찰 서버 연결은 묶음에서 다시 씀 — 쉬던 연결이 끊겼으면 한 번만 다시, 시간 초과는 다시 안 함, 큰 원본·오류 응답은 연결을 닫음."""
    import urllib3

    from apps.api import thumbs

    class Resp:
        def __init__(self, status=200, body=b"jpg"):
            self.status, self.body, self.closed, self.released = status, body, False, False

        def read(self, n):
            return self.body[:n]

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    class Pool:
        def __init__(self, *outcomes):
            self.outcomes, self.calls = list(outcomes), 0

        def request(self, method, url, preload_content=True):
            assert method == "GET" and preload_content is False   # 끝까지 다 읽어야 연결을 돌려줌
            self.calls += 1
            o = self.outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o

    dropped = urllib3.exceptions.MaxRetryError(None, _POLICE_IMG, urllib3.exceptions.ProtocolError("Connection aborted."))
    ok = Resp()
    monkeypatch.setattr(thumbs, "_POOL", Pool(dropped, ok))
    assert thumbs._fetch(_POLICE_IMG) == b"jpg" and thumbs._POOL.calls == 2
    assert ok.released and not ok.closed                              # 다 읽은 연결은 묶음으로 돌아감

    timeout = urllib3.exceptions.MaxRetryError(None, _POLICE_IMG, urllib3.exceptions.ReadTimeoutError(None, _POLICE_IMG, "timed out"))
    monkeypatch.setattr(thumbs, "_POOL", Pool(timeout, Resp()))
    with pytest.raises(urllib3.exceptions.MaxRetryError):
        thumbs._fetch(_POLICE_IMG)
    assert thumbs._POOL.calls == 1                                    # 시간 초과는 다시 안 함(방문자가 오래 기다리지 않게)

    for bad in (Resp(status=404), Resp(body=b"x" * (thumbs.MAX_SRC + 2))):
        monkeypatch.setattr(thumbs, "_POOL", Pool(bad))
        with pytest.raises((OSError, ValueError)):
            thumbs._fetch(_POLICE_IMG)
        assert bad.closed and bad.released                            # 다 안 읽은 연결은 닫고 돌려보냄(다음엔 새 연결)


def test_thumb_sweep_removes_only_gone_and_old(monkeypatch, tmp_path):
    """색인에서 빠진 습득물의 사진만 지움 — 방금 만든 것·아직 있는 것은 두고, 지울 게 너무 많으면 멈춤."""
    import os as _os
    import time as _time

    from apps.api import thumbs
    monkeypatch.setattr(thumbs, "CACHE_DIR", tmp_path)
    keep, gone, fresh = (_POLICE_IMG.replace("/1.do", f"/{x}.do") for x in ("k", "g", "f"))
    old = _time.time() - 7200
    for u in (keep, gone, fresh):
        (tmp_path / thumbs._name(u)).write_bytes(b"x")
    for u in (keep, gone):
        _os.utime(tmp_path / thumbs._name(u), (old, old))
    st = thumbs.sweep([keep])
    assert st["removed"] == 1 and not (tmp_path / thumbs._name(gone)).exists()
    assert (tmp_path / thumbs._name(keep)).exists() and (tmp_path / thumbs._name(fresh)).exists()
    # 색인 조회가 잘못돼 대부분을 지우게 되면 하나도 안 지움
    for i in range(1200):
        f = tmp_path / f"{i:040d}.jpg"
        f.write_bytes(b"x")
        _os.utime(f, (old, old))
    assert thumbs.sweep([keep])["skipped"] and len(list(tmp_path.iterdir())) == 1202


def test_email_photo_uses_small_thumbnail():
    from apps.notifier import build_email
    m = {"atc_id": "A_1", "name": "지갑", "color": "검정", "grade": 90, "image_url": _POLICE_IMG}
    _, _, html = build_email({"id": "x", "text": "검정 지갑", "lang": "ko"}, [m])
    assert "/thumb.jpg?u=https%3A%2F%2Fminwon24.police.go.kr%2F" in html and f'src="{_POLICE_IMG}"' not in html
