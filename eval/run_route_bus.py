"""버스 번호 → 경유 구 대조 — Gemini 노선 추정(운영 코드 그대로) vs 서울 버스노선 API 실제 경유 구.

질문: 경유 지역을 지금처럼 Gemini 지식으로 추정해도 되나, 실제 노선 API로 바꿔야 하나(버스 번호가 Gemini가 가장 약할 곳).

- 정답: 서울 버스노선 API(공공데이터포털 키) 정류장 좌표 → 카카오 좌표→행정구역 → 노선이 실제로 지나는 구/시.
- 표본: 서울 버스 전체 노선 목록에서 종류별 무작위(시드 고정) — 이름난 노선만 고르는 치우침을 피함.
- Gemini: apps/agent/route.LLMRouteResolver().expand — 운영과 같은 프롬프트·모델·thinking 설정·20개 자르기.
  장소 표현은 추출 단계가 내는 꼴('146번 버스')과 시도를 붙인 꼴('서울 146번 버스') 두 가지.
- 판정: apps/api/region.region_matches(운영 가점과 같은 함수).
  · 실제 구 적중률 = 실제 경유 구 중 Gemini 목록에 든 비율(놓치면 그 구의 습득물이 가점을 못 받음)
  · 정밀도 = Gemini 구 중 실제로 지나는 비율(프롬프트가 '인접'까지 넓게 달라고 하므로 낮은 게 정상)
  · 기점 구 적중 = 첫 정류장(대개 차고지 쪽) 구 — 버스 습득물은 운수사 차고지로 가므로 가장 중요
  · 회차 구 적중 = 회차 정류장 구

  uv run python -m eval.run_route_bus                # 수집(캐시) → Gemini → 보고서
  uv run python -m eval.run_route_bus --report-only  # 캐시로 보고서만

공공 API·카카오 응답과 Gemini 답은 eval/data/route_bus.json에 캐시(다시 돌리면 호출 없음, --fresh-gemini로 Gemini만 다시).
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.api.region import region_matches
from scripts.build_bus_routes import kakao_region as _kakao_region
from scripts.build_bus_routes import seoul_bus as _bus

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "route_bus.json"
OUT = ROOT / "results" / "route_bus.md"
SEED = 20261116
# 종류(routeType) → 표본 수. 서울 버스 API 코드: 1 공항 2 마을 3 간선 4 지선 5 순환 6 광역 (7 인천·8 경기·9 폐지 제외)
STRATA = {"간선": 10, "지선": 10, "광역": 4, "마을": 6, "순환": 2, "공항": 2, "심야": 3}
TYPE_NAME = {"1": "공항", "2": "마을", "3": "간선", "4": "지선", "5": "순환", "6": "광역"}
PHRASES = {"bare1": "{n}번 버스", "bare2": "{n}번 버스", "seoul": "서울 {n}번 버스"}
LABELS = {**{t: f"Gemini `{f.format(n='N')}` ({t})" for t, f in PHRASES.items()},
          "table": "**노선표** `N번 버스` (운영 resolver)"}


def _kind(r: dict) -> str | None:
    if r["busRouteNm"].startswith("N"):
        return "심야"
    return TYPE_NAME.get(r["routeType"])


def fetch(cache: dict) -> None:
    if "sample" in cache:
        return
    routes: dict[str, dict] = {}
    for d in "0123456789":
        for r in _bus("getBusRouteList", {"strSrch": d}):
            routes[r["busRouteId"]] = r
    by_kind: dict[str, list[dict]] = {}
    for r in routes.values():
        k = _kind(r)
        if k:
            by_kind.setdefault(k, []).append(r)
    print("전체 노선:", len(routes), {k: len(v) for k, v in sorted(by_kind.items())})
    rng = random.Random(SEED)
    sample = []
    for k, n in STRATA.items():
        pool = sorted(by_kind.get(k, []), key=lambda r: r["busRouteNm"])
        for r in rng.sample(pool, min(n, len(pool))):
            sample.append({"id": r["busRouteId"], "name": r["busRouteNm"], "kind": k,
                           "start": r.get("stStationNm", ""), "end": r.get("edStationNm", ""),
                           "corp": r.get("corpNm", "").split("  ")[0]})
    coords: dict[str, str] = cache.setdefault("coords", {})
    for s in sample:
        st = sorted(_bus("getStaionByRoute", {"busRouteId": s["id"]}), key=lambda x: int(x.get("seq") or 0))
        s["stations"] = [{"nm": x.get("stationNm", ""), "x": x.get("gpsX", ""), "y": x.get("gpsY", ""),
                          "trans": x.get("transYn", "")} for x in st]
    todo = sorted({f"{float(p['x']):.4f},{float(p['y']):.4f}" for s in sample for p in s["stations"] if p["x"] and p["y"]}
                  - set(coords))
    print("카카오 좌표 조회:", len(todo))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, reg in zip(todo, ex.map(lambda k: _kakao_region(*map(float, k.split(","))), todo), strict=True):
            coords[key] = reg
    for s in sample:
        for p in s["stations"]:
            p["region"] = coords.get(f"{float(p['x']):.4f},{float(p['y']):.4f}", "") if p["x"] and p["y"] else ""
    cache["sample"] = sample


def run_gemini(cache: dict, fresh: bool) -> None:
    from apps.agent.route import LLMRouteResolver

    gem = cache.setdefault("gemini", {})
    for tag, fmt in PHRASES.items():
        if tag in gem and not fresh:
            continue
        res = LLMRouteResolver()  # 꼴마다 새로(같은 장소 캐시가 다른 회차에 섞이지 않게)
        names = [s["name"] for s in cache["sample"]]

        def one(n: str, res=res, fmt=fmt) -> list[str]:
            try:
                return res.expand(fmt.format(n=n))
            except Exception as e:  # 운영에선 노드가 실패를 삼킴 — 측정에선 빈 결과로 셈
                print("  실패", n, type(e).__name__)
                return []

        with ThreadPoolExecutor(max_workers=6) as ex:
            gem[tag] = dict(zip(names, ex.map(one, names), strict=True))
        print(f"Gemini {tag}: {sum(1 for v in gem[tag].values() if v)}/{len(names)} 응답")


def run_table(cache: dict) -> tuple[dict, list[str]]:
    """운영 resolver(노선표 우선)를 같은 표현 'N번 버스'로 — Gemini 대신 기록만 하는 가짜를 붙여 넘긴 노선을 셈(호출·비용 0)."""
    from apps.agent import route

    passed: list[str] = []

    class _Rec:
        def expand(self, place: str) -> list[str]:
            passed.append(place)
            return []

    r = route.HybridRouteResolver(route.BusTable.load(route.settings.data_dir / "bus_routes.json"), _Rec())
    return {s["name"]: r.expand(PHRASES["bare1"].format(n=s["name"])) for s in cache["sample"]}, passed


def _uniq(xs):
    return list(dict.fromkeys(x for x in xs if x))


# 인천 행정구역 개편(2026-07) — 카카오는 새 이름(영종구·서해구…)을 주지만 색인·Gemini는 옛 이름(중구·서구)이 섞여 있음.
# 측정에선 같은 곳으로 봄(어느 쪽 이름을 써도 맞은 것으로).
_INCHEON_OLD = {"영종구": ("중구",), "제물포구": ("중구", "동구"), "서해구": ("서구",), "검단구": ("서구",)}


def _aliases(region: str) -> list[str]:
    t = region.split()
    if len(t) == 2 and t[0] == "인천광역시" and t[1] in _INCHEON_OLD:
        return [region] + [f"인천광역시 {o}" for o in _INCHEON_OLD[t[1]]]
    return [region]


def _hit(region: str, gem: list[str]) -> bool:
    return bool(region) and any(region_matches(a, gem) for a in _aliases(region))


def score(s: dict, gem: list[str]) -> dict:
    real = _uniq(p["region"] for p in s["stations"])
    st = s["stations"]
    start = st[0]["region"] if st else ""
    turn = next((p["region"] for p in st if p["trans"] == "Y"), "") or next(
        (p["region"] for p in st if p["nm"] == s["end"]), "")
    covered = [r for r in real if _hit(r, gem)]
    good = [g for g in gem if any(_hit(r, [g]) for r in real)]
    return {"real": real, "n_real": len(real), "n_gem": len(gem),
            "recall": len(covered) / len(real) if real else 0.0,
            "precision": len(good) / len(gem) if gem else 0.0,
            "start": start, "start_hit": _hit(start, gem),
            "turn": turn, "turn_hit": _hit(turn, gem),
            "missed": [r for r in real if r not in covered], "extra": [g for g in gem if g not in good],
            "other_city": any(g.split()[0] not in _CAPITAL for g in gem if g.split())}


_CAPITAL = {"서울", "경기", "인천", "서울특별시", "경기도", "인천광역시"}


def _short(r: str) -> str:
    t = r.split()
    return " ".join(t[1:]) if t and t[0] == "서울특별시" else r


def report(cache: dict) -> str:
    sample, gem = cache["sample"], cache["gemini"]
    lines = ["# 버스 번호 → 경유 구: Gemini 추정 vs 서울 버스노선 API", "",
             f"측정일 {time.strftime('%Y-%m-%d')} · 표본 {len(sample)}개 노선(종류별 무작위, 시드 {SEED}) · "
             "판정은 운영 가점과 같은 `region_matches` · 스크립트 `eval/run_route_bus.py`", ""]
    kinds = list(STRATA)
    table, passed = run_table(cache)
    gem = {**gem, "table": table}
    tags = [*PHRASES, "table"]
    rows = {tag: [score(s, gem[tag].get(s["name"], [])) for s in sample] for tag in tags}

    def agg(sc: list[dict]) -> str:
        n = len(sc)
        if not n:
            return "| - |"
        m = lambda k: sum(x[k] for x in sc) / n  # noqa: E731
        return (f"{n} | {m('recall'):.0%} | {sum(x['start_hit'] for x in sc)}/{n} | {sum(x['turn_hit'] for x in sc)}/{n} | "
                f"{sum(1 for x in sc if x['recall'] == 0)} | {m('precision'):.0%} | {m('n_real'):.1f} → {m('n_gem'):.1f} | "
                f"{sum(x['other_city'] for x in sc)} | {sum(x['n_gem'] >= 15 for x in sc)} |")

    head = ("| 실제 구 적중률 | 기점 구 적중 | 회차 구 적중 | 전부 빗나감 | 정밀도 | 구 개수(실제→Gemini) "
            "| 수도권 밖 도시 섞임 | 15개 이상 나열 |")
    lines += ["## 요약", "",
              "표본 노선은 모두 수도권(서울·경기·인천) 안을 다님 → '수도권 밖 도시 섞임'은 번호만 보고 다른 도시 버스로 헷갈린 경우.",
              "'15개 이상 나열'은 노선을 몰라 넓게 뿌린 경우가 많음(적중률을 부풀림 — 정밀도와 함께 봐야 함).", "",
              "| 장소 표현 | 노선 " + head, "|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|"]
    for tag in tags:
        lines.append(f"| {LABELS[tag]} | " + agg(rows[tag]))
    lines += ["", f"노선표 줄: 채점 기준이 같은 노선 API라 적중률 100%는 당연 — 운영 resolver가 번호를 알아보는지 확인용. "
              f"Gemini로 넘긴 노선 **{len(passed)}개**{(' (' + ', '.join(passed) + ')') if passed else ''}."]
    lines += ["", "## 종류별 (`N번 버스`, 1회차)", "",
              "| 종류 | 노선 " + head, "|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|"]
    for k in kinds:
        sc = [x for s, x in zip(sample, rows["bare1"], strict=True) if s["kind"] == k]
        if sc:
            lines.append(f"| {k} | " + agg(sc))
    # 같은 표현 두 번 — 답이 얼마나 흔들리나
    jac = []
    for s in sample:
        a, b = set(gem["bare1"].get(s["name"], [])), set(gem["bare2"].get(s["name"], []))
        if a | b:
            jac.append(len(a & b) / len(a | b))
    lines += ["", f"같은 표현 두 번 물었을 때 구 목록 겹침(자카드) 평균 **{sum(jac) / len(jac):.0%}** "
              "(운영은 같은 장소를 메모리 캐시하므로 재시작 전까진 한 답으로 고정)." if jac else ""]
    lines += ["", "## 노선별 (`N번 버스`, 1회차)", "",
              "| 노선 | 종류 | 기점 → 종점 | 실제 구 | 적중 | 기점 | 회차 | 놓친 구 | Gemini가 더 넣은 구 |",
              "|---|---|---|:-:|:-:|:-:|:-:|---|---|"]
    for s, x in zip(sample, rows["bare1"], strict=True):
        ok = lambda b: "O" if b else "X"  # noqa: E731
        lines.append(f"| {s['name']} | {s['kind']} | {s['start']} → {s['end']} | {x['n_real']} | {x['recall']:.0%} | "
                     f"{ok(x['start_hit'])} {_short(x['start'])} | {ok(x['turn_hit'])} {_short(x['turn'])} | "
                     f"{', '.join(_short(r) for r in x['missed'][:5])}{' …' if len(x['missed']) > 5 else ''} | "
                     f"{', '.join(x['extra'][:5])}{' …' if len(x['extra']) > 5 else ''} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--fresh-gemini", action="store_true")
    a = ap.parse_args()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    if not a.report_only:
        fetch(cache)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        run_gemini(cache, a.fresh_gemini)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    md = report(cache)
    OUT.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
