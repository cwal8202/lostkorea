"""AI 비용·정확도·운영 — thinking 수준 설정, 모델 종료(404) 시 예비 모델 자동 전환, 노선 끝(도착지) 보존, 같은 장소 캐시.

외부 서비스 없음: Gemini HTTP는 목킹.
"""

import io
import json
import urllib.error

import pytest

from apps import ops
from apps.agent import route
from apps.api import gemini
from apps.api.config import settings


@pytest.fixture(autouse=True)
def reset_model():
    gemini._ACTIVE["model"] = None
    yield
    gemini._ACTIVE["model"] = None


def _ok(payload: dict):
    return io.BytesIO(json.dumps(payload).encode())


def _gen_response(obj) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}


def test_generation_requests_carry_thinking_level(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return _ok(_gen_response({"regions": ["서울 마포"]}))

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    gemini.generate_json("x")
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    monkeypatch.setattr(settings, "gemini_thinking_level", "")
    gemini.generate_json("x")
    assert "thinkingConfig" not in sent[1]["generationConfig"]   # 빈 값이면 모델 기본값


def test_retired_model_switches_to_fallback_and_alerts(monkeypatch):
    urls, alerts = [], []

    def fake_urlopen(req, timeout=0):
        urls.append(req.full_url)
        if settings.gemini_grade_model in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "model not found", {}, None)
        return _ok(_gen_response({"ok": True}))

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ops, "alert", lambda s, b: alerts.append(s))
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    import threading
    monkeypatch.setattr(threading, "Thread",
                        lambda target, daemon, args: type("T", (), {"start": lambda self: target(*args)})())

    assert gemini.generate_json("x") == {"ok": True}
    assert gemini.generate_json("y") == {"ok": True}            # 이후 호출은 처음부터 예비 모델로
    assert [settings.gemini_fallback_model in u for u in urls] == [False, True, True]
    assert len(alerts) == 1 and "모델 전환" in alerts[0]


def test_long_route_keeps_destination(monkeypatch):
    """서울역→부산역 KTX처럼 긴 노선: 앞에서만 자르면 도착지(부산)가 잘렸다 → 앞뒤를 남김."""
    regions = ["서울 중구"] + [f"경유{i}" for i in range(30)] + ["부산 동구"]
    monkeypatch.setattr(route.gemini, "generate_json", lambda p: {"regions": regions})
    out = route.LLMRouteResolver().expand("서울역에서 부산역 가는 KTX")
    assert len(out) == route.MAX_REGIONS and out[0] == "서울 중구" and out[-1] == "부산 동구"


def test_same_place_reuses_cached_regions(monkeypatch):
    calls = []
    monkeypatch.setattr(route.gemini, "generate_json",
                        lambda p: calls.append(p) or {"regions": ["서울 마포", "서울 마포", "서울 서대문"]})
    r = route.LLMRouteResolver()
    assert r.expand("홍대입구역") == ["서울 마포", "서울 서대문"]       # 중복 제거
    assert r.expand("  홍대입구역 ") == ["서울 마포", "서울 서대문"]     # 공백 차이도 같은 장소
    assert len(calls) == 1


def test_failed_route_is_not_cached(monkeypatch):
    results = iter([None, {"regions": ["서울 강남"]}])
    monkeypatch.setattr(route.gemini, "generate_json", lambda p: next(results))
    r = route.LLMRouteResolver()
    assert r.expand("강남역") == []
    assert r.expand("강남역") == ["서울 강남"]                            # 실패는 다음에 다시 시도


def test_model_rejecting_thinking_level_steps_down(monkeypatch):
    """모델마다 받는 thinking 수준이 다름(3.8-flash는 minimal을 400으로 거부) → low → 생략 순으로 자동 조정, 모델별 기억."""
    seen = []

    def fake_urlopen(req, timeout=0):
        tc = json.loads(req.data)["generationConfig"].get("thinkingConfig")
        seen.append(tc and tc["thinkingLevel"])
        if tc and tc["thinkingLevel"] in ("minimal", "low"):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                         io.BytesIO(b'{"error":{"message":"Thinking level MINIMAL is not supported"}}'))
        return _ok(_gen_response({"ok": True}))

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    gemini._THINK_OK.clear()
    assert gemini.generate_json("x") == {"ok": True}
    assert seen == ["minimal", "low", None]
    assert gemini.generate_json("y") == {"ok": True}
    assert seen[3:] == [None]                                   # 다음부터는 바로 받아주는 설정으로
    gemini._THINK_OK.clear()


def test_other_400_errors_are_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error":{"message":"invalid json"}}'))

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    assert gemini.generate_json("x") is None and calls == [1]


def test_visual_compare_runs_in_parallel_and_keeps_rank_order(monkeypatch):
    """사진 대조 3장을 동시에(순차면 3배 시간). 받기 실패한 후보는 건너뛰고 순위 순서로 처음 성공한 3장만 대조."""
    import time

    from apps.agent import graph
    ms = [{"atc_id": str(i), "image_url": f"https://img/{i}.jpg"} for i in range(6)]
    monkeypatch.setattr(graph, "_valid_img", lambda u: True)
    monkeypatch.setattr(graph, "_fetch_image_b64", lambda u: (None, "") if u.endswith("/1.jpg") else ("b64", "image/jpeg"))

    def slow_compare(*a, **k):
        time.sleep(0.3)
        return {"score": 77, "reason": "비슷"}

    monkeypatch.setattr(graph.gemini, "compare_images", slow_compare)
    t0 = time.time()
    graph._visual_compare("u", "image/jpeg", ms, top=3)
    assert time.time() - t0 < 0.8                                  # 순차였다면 0.9초 이상
    assert [m["atc_id"] for m in ms if "visual_score" in m] == ["0", "2", "3"]


def test_generation_tokens_are_metered(monkeypatch):
    from apps import store
    store.init()
    store._run("DELETE FROM metrics")
    res = _gen_response({"ok": True})
    res["usageMetadata"] = {"promptTokenCount": 1200, "candidatesTokenCount": 500, "thoughtsTokenCount": 30}
    monkeypatch.setattr(gemini.urllib.request, "urlopen", lambda req, timeout=0: _ok(res))
    gemini.generate_json("x")
    gemini.generate_json("y")
    assert store.metrics_since("2000-01-01") == {"ai_calls": 2, "ai_in": 2400, "ai_out": 1060}
