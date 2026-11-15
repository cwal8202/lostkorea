"""지명 사전으로 확실히 못 푸는 보관장소만 카카오 장소 검색으로 풀어 eval/data/dep_geocoded.json(지역 해석 캐시)에 채움.

대상 = 지역 해석기(apps/api/region.py)가 '낱말 속 지명'(예: 더현대서울·이마트해운대점·다대포항역)이나 아무것도 못 찾은 보관장소.
이런 곳은 짧은 지명이 맞기도 틀리기도 해서(다대포항→포항, 월드컵경기장→기장) 실제 장소 검색 결과를 먼저 쓴다.
캐시는 저장소에 커밋 — 검색 가점·수집·백필이 같은 값을 씀. 공공기관·상호 이름만 보냄(개인정보 없음). 카카오 무료 한도 일 10만.

  uv run python -m scripts.build_geocode_cache                      # 색인(settings.opensearch_url)의 보관장소 전부
  uv run python -m scripts.build_geocode_cache --deps-file deps.json  # 보관장소 목록 파일(문자열 배열 또는 terms 집계 결과)
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

from apps.api.config import settings
from apps.api.region import resolver
from scripts.geocode_region import kakao_region

CACHE = settings.data_dir / "dep_geocoded.json"


def deps_from_index() -> list[str]:
    body = json.dumps({"size": 0, "aggs": {"d": {"terms": {"field": "dep_place", "size": 50000}}}}).encode()
    req = urllib.request.Request(f"{settings.opensearch_url}/{settings.opensearch_index}/_search", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return [b["key"] for b in json.load(r)["aggregations"]["d"]["buckets"]]


def deps_from_file(path: str) -> list[str]:
    data = json.loads(open(path, encoding="utf-8").read())
    if isinstance(data, list):
        return [str(x) for x in data]
    return [b["key"] for b in data["aggregations"]["d"]["buckets"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deps-file")
    ap.add_argument("--refresh", action="store_true", help="이미 캐시에 있는 것도 다시 조회")
    args = ap.parse_args()
    key = settings.kakao_rest_api_key
    if not key:
        raise SystemExit("KAKAO_REST_API_KEY 미설정")
    deps = deps_from_file(args.deps_file) if args.deps_file else deps_from_index()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    targets = [d for d in deps if (args.refresh or d not in cache)
               and (resolver.resolve_detail(d)[1] in ("embedded", "none", "geocoded")
                    or not resolver.has_sido(resolver.resolve(d)))]
    print(f"보관장소 {len(deps)}곳 중 장소 검색 대상 {len(targets)}곳(낱말 속 지명·미해석)")
    found = 0
    for i, dep in enumerate(targets, 1):
        reg = kakao_region(dep, key)
        cache[dep] = resolver.canonical(reg, dep) if reg else ""
        found += bool(reg)
        if i % 100 == 0:
            print(f"  {i}/{len(targets)} (찾음 {found})", flush=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
        time.sleep(0.05)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    print(f"완료: {found}/{len(targets)}곳 찾음 → {CACHE}")


if __name__ == "__main__":
    main()
