"""버스 노선표 만들기 — 노선번호 → 실제로 지나는 시군구(+기점·운수사). 경유 지역 가점(apps/agent/route.py)이 씀.

왜: 경유 지역을 Gemini 지식으로 추정하면 세 자리 간선·광역·심야·공항 버스에서 다른 도시로 헷갈리거나(102번 → 부산)
모르는 노선은 구를 15~20개씩 뿌렸다(eval/results/route_bus.md: 실제 3.9개 → Gemini 10개, 정밀도 38%).
실제 노선 정류장 좌표 → 카카오 좌표→행정구역으로 정확한 구 목록을 미리 만들어 둔다.

- 출처: 서울 버스노선 API(공공데이터포털 키) — 서울 시내버스 + 서울에 들어오는 경기·인천 버스(590·38개, 정류장 전 구간).
  다른 지역은 국토교통부 API 활용 승인 뒤 같은 표에 출처를 더함.
- 신고 처리 중에는 API를 부르지 않음 — 이 표(eval/data/bus_routes.json)를 저장소에 커밋해 배포와 함께 실음.
  서버에서 만들지 말 것(서버 파일이 바뀌면 자동배포의 git pull이 실패) → 로컬에서 만들어 커밋.
- 갱신: 월 1회 정도(노선 개편·신설). 좌표→구는 eval/data/bus_coord_regions.json에 캐시 → 다음부턴 새 정류장만 카카오 조회.
- 이어하기: 노선별로 받은 날짜를 남김 — 공공 API 하루 한도에 걸려 멈추면 다음 실행이 남은 노선부터.

  uv run python -m scripts.build_bus_routes              # 받은 지 25일 넘은 노선만 다시(처음엔 전부)
  uv run python -m scripts.build_bus_routes --force      # 전부 다시
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from apps.api.config import settings

DATA = settings.data_dir
TABLE = DATA / "bus_routes.json"
COORDS = DATA / "bus_coord_regions.json"
SEOUL_BUS = "http://ws.bus.go.kr/api/rest/busRouteInfo"
KAKAO = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"

# 서울 버스 API routeType → 종류. 7 인천·8 경기는 서울에 들어오는 노선(이름 끝에 도시: '320안산'). 9 폐지는 뺌.
SEOUL_KIND = {"1": "공항", "2": "마을", "3": "간선", "4": "지선", "5": "순환", "6": "광역", "7": "인천", "8": "경기",
              "10": "투어", "15": "심야"}
_SIDO_SHORT = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원특별자치도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라남도": "전남", "경상북도": "경북",
    "경상남도": "경남", "제주특별자치도": "제주",
}
# 인천 행정구역 개편(2026-07): 카카오는 새 이름, 습득물 색인엔 옛 이름(영종지구대 → 중구)이 섞여 있음 → 둘 다 넣음
_INCHEON_OLD = {"영종구": ("중구",), "제물포구": ("중구", "동구"), "서해구": ("서구",), "검단구": ("서구",)}
_TAGS = re.compile(r"\(.*?\)|출근|퇴근|예약|급행하행|급행|거점")


class ApiStop(Exception):
    """공공 API가 거절(한도 초과·키 오류 등) — 지금까지 받은 것만 저장하고 멈춤."""


def seoul_bus(op: str, params: dict) -> list[dict]:
    """서울 버스노선 API 호출 → itemList 행들. headerCd 0 정상, 4 결과 없음, 그 밖은 ApiStop."""
    q = urllib.parse.urlencode({**params, "serviceKey": settings.data_go_kr_service_key_decoded})
    for i in range(4):
        try:
            with urllib.request.urlopen(f"{SEOUL_BUS}/{op}?{q}", timeout=20) as r:
                root = ET.fromstring(r.read())
        except urllib.error.HTTPError as e:
            raise ApiStop(f"{op} HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ET.ParseError):
            time.sleep(1 + i)
            continue
        code = root.findtext(".//headerCd")
        if code not in ("0", "4"):
            raise ApiStop(f"{op} headerCd={code} {root.findtext('.//headerMsg')}")
        return [{c.tag: (c.text or "") for c in it} for it in root.findall(".//itemList")]
    raise ApiStop(f"{op} 응답 없음(네트워크)")


def kakao_region(x: float, y: float) -> str:
    """좌표 → '시도 시군구'(카카오 법정동 기준, 원래 이름 그대로 — 정리는 region_names). 실패면 ''."""
    url = f"{KAKAO}?{urllib.parse.urlencode({'x': x, 'y': y})}"
    req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {settings.kakao_rest_api_key}"})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                docs = json.loads(r.read()).get("documents", [])
            d = next((d for d in docs if d.get("region_type") == "B"), docs[0] if docs else None)
            return f"{d['region_1depth_name']} {d['region_2depth_name']}".strip() if d else ""
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
            time.sleep(1 + i)
    return ""


def region_names(raw: str) -> list[str]:
    """'경기도 고양시 일산서구' → ['경기 고양시'], '인천광역시 영종구' → ['인천 영종구', '인천 중구'].

    시도는 줄임말(경유 지역 칩에 보이는 꼴, region_matches가 그대로 비교), 시 아래 구는 뗌 — 색인에 '경기도 고양시'로만 된
    습득물도 맞도록. 세종처럼 시군구가 없으면 시도만.
    """
    t = raw.split()
    if not t:
        return []
    sido = t[0]
    if sido == "전남광주통합특별시" and len(t) > 1:  # 카카오 새 이름 — 색인 표기(apps/api/region.py와 같은 처리)
        sido = "광주광역시" if t[1] in {"광산구", "동구", "서구", "남구", "북구"} else "전라남도"
    short = _SIDO_SHORT.get(sido, sido)
    if len(t) < 2:
        return [short]
    sgg = t[1]
    out = [f"{short} {sgg}"]
    if sido == "인천광역시" and sgg in _INCHEON_OLD:
        out += [f"인천 {o}" for o in _INCHEON_OLD[sgg]]
    return out


def route_key(name: str, rtype: str) -> tuple[str, str]:
    """(찾기 열쇠, 도시). '146' → ('146','서울'), '320안산' → ('320','안산'), '5001(예약)용인' → ('5001','용인'),
    '서울01퇴근' → ('서울01','서울'), 'N37' → ('N37','서울'). 사람은 번호로 말하므로 꼬리표·도시를 뗀 번호가 열쇠."""
    city = "서울"
    base = name
    if rtype in ("7", "8"):
        m = re.match(r"^[A-Za-z]?\d+(?:-\d+)?[A-Za-z]?", name)
        if m:
            base, rest = m.group(), name[m.end():]
            city = _TAGS.sub("", rest).strip() or ("인천" if rtype == "7" else "경기")
    return _TAGS.sub("", base).strip().upper(), city


def _coord_key(x: str, y: str) -> str | None:
    try:
        return f"{float(x):.4f},{float(y):.4f}"
    except ValueError:
        return None


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def build_seoul(force: bool = False, max_age_days: int = 25) -> None:
    table = _load(TABLE, {"routes": {}})
    routes_out: dict = table["routes"]
    coords: dict = _load(COORDS, {})
    today = date.today()
    fresh_after = (today - timedelta(days=max_age_days)).isoformat()

    listed: dict[str, dict] = {}
    for d in "0123456789":  # 노선번호 일부 검색 — 모든 노선에 숫자가 있음
        for r in seoul_bus("getBusRouteList", {"strSrch": d}):
            if r.get("routeType") in SEOUL_KIND:
                listed[r["busRouteId"]] = r
    print(f"서울 버스 API 노선 {len(listed)}개")
    gone = [k for k in routes_out if k.startswith("seoul:") and k[6:] not in listed]
    for k in gone:  # 폐지·번호 바뀐 노선
        del routes_out[k]
    todo = [r for rid, r in listed.items()
            if force or routes_out.get(f"seoul:{rid}", {}).get("fetched", "") < fresh_after]
    print(f"정류장 받을 노선 {len(todo)}개 (빠진 노선 {len(gone)}개 지움)")

    stations: dict[str, list[dict]] = {}
    stopped = ""
    for i, r in enumerate(todo, 1):
        try:
            stations[r["busRouteId"]] = sorted(seoul_bus("getStaionByRoute", {"busRouteId": r["busRouteId"]}),
                                               key=lambda s: int(s.get("seq") or 0))
        except ApiStop as e:
            stopped = str(e)
            print(f"  {i - 1}/{len(todo)}에서 멈춤: {e} — 다음 실행이 이어서 받음")
            break
        if i % 100 == 0:
            print(f"  정류장 {i}/{len(todo)}")

    need = sorted({k for st in stations.values() for s in st
                   if (k := _coord_key(s.get("gpsX", ""), s.get("gpsY", ""))) and k not in coords})
    print(f"카카오 좌표 조회 {len(need)}개 (캐시 {len(coords)}개)")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, reg in zip(need, ex.map(lambda k: kakao_region(*map(float, k.split(","))), need), strict=True):
            if reg:
                coords[k] = reg
    _save(COORDS, coords)

    for rid, st in stations.items():
        r = listed[rid]
        regs: list[str] = []
        for s in st:
            k = _coord_key(s.get("gpsX", ""), s.get("gpsY", ""))
            for name in region_names(coords.get(k, "")) if k else []:
                if name not in regs:
                    regs.append(name)
        if not regs:
            continue
        first = _coord_key(st[0].get("gpsX", ""), st[0].get("gpsY", "")) if st else None
        key, city = route_key(r["busRouteNm"], r["routeType"])
        routes_out[f"seoul:{rid}"] = {
            "name": r["busRouteNm"], "key": key, "city": city, "kind": SEOUL_KIND[r["routeType"]],
            "regions": regs, "start_region": (region_names(coords.get(first, "")) or [""])[0] if first else "",
            "corp": (r.get("corpNm") or "").split("  ")[0].strip(),
            "start": r.get("stStationNm", ""), "end": r.get("edStationNm", ""), "fetched": today.isoformat(),
        }
    table["_note"] = ("노선번호 → 실제로 지나는 시군구. scripts/build_bus_routes.py가 만듦(직접 고치지 말 것). "
                      "출처 seoul = 서울 버스노선 API(서울 시내 + 서울에 들어오는 경기·인천 버스).")
    _save(TABLE, table)
    done = sum(1 for k in routes_out if k.startswith("seoul:"))
    print(f"노선표 저장: 서울 출처 {done}/{len(listed)}개" + (f" — 미완료({stopped})" if stopped else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="받은 날짜와 상관없이 전부 다시")
    ap.add_argument("--max-age-days", type=int, default=25)
    a = ap.parse_args()
    build_seoul(force=a.force, max_age_days=a.max_age_days)


if __name__ == "__main__":
    main()
