"""버스 노선표 만들기 — 노선번호 → 실제로 지나는 시군구(+기점·운수사). 경유 지역 가점(apps/agent/route.py)이 씀.

왜: 경유 지역을 Gemini 지식으로 추정하면 세 자리 간선·광역·심야·공항 버스에서 다른 도시로 헷갈리거나(102번 → 부산)
모르는 노선은 구를 15~20개씩 뿌렸다(eval/results/route_bus.md: 실제 3.9개 → Gemini 10개, 정밀도 38%).
실제 노선 정류장 좌표 → 카카오 좌표→행정구역으로 정확한 구 목록을 미리 만들어 둔다.

- 출처 seoul: 서울 버스노선 API(공공데이터포털 키) — 서울 시내버스 + 서울에 들어오는 경기·인천 버스(정류장 전 구간).
  정류장 좌표 → 카카오로 구.
- 출처 molit: 국토교통부 '버스노선별 경유정류장' API — 전국. 정류장마다 시도·시군구가 붙어 와서 카카오 조회가 필요 없음.
  단 **시군구 단위로만** 물을 수 있어(그 시군구 안 정류장만 옴) 전국 시군구를 돌며 노선별로 합침.
  서울 출처에 이미 있는 노선(같은 번호·같은 구간)은 빼고 넣음 — 서울 출처가 노선 종류(간선·광역…)까지 있음.
  자료는 운행일자별이고 몇 달 늦게 올라옴(2026-11 기준 6월까지) → 자료가 있는 가장 최근 날짜를 찾아 씀.
- 출처 tago: TAGO 버스노선정보(같은 키) — 국토부 자료에 거의 없는 대구·광주 시내버스(11/18 실측 대구 0개). 노선 목록 →
  노선별 정류장 좌표 → 카카오로 구. 국토부 출처의 같은 노선은 뺌.
- 신고 처리 중에는 API를 부르지 않음 — 이 표(eval/data/bus_routes.json)를 저장소에 커밋해 배포와 함께 실음.
  서버에서 만들지 말 것(서버 파일이 바뀌면 자동배포의 git pull이 실패) → 로컬에서 만들어 커밋.
- 갱신: 월 1회 정도(노선 개편·신설). 좌표→구는 eval/data/bus_coord_regions.json에 캐시 → 다음부턴 새 정류장만 카카오 조회.
- 이어하기: 서울은 노선별로 받은 날짜를, 전국은 시군구별 결과를 data/bus_molit/, 대구·광주는 노선별 정류장을 data/bus_tago/
  (둘 다 커밋 안 함)에 남김 — 공공 API 하루 한도에
  걸려 멈추면 다음 실행이 남은 것부터.

  uv run python -m scripts.build_bus_routes              # 서울·대구·광주(받은 지 25일 넘은 노선만) + 전국(새 운행일자면 다시)
  uv run python -m scripts.build_bus_routes --only molit # 전국만(--only tago: 대구·광주만)
  uv run python -m scripts.build_bus_routes --force      # 전부 다시
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from apps.api.config import settings
from apps.api.region import region_matches

DATA = settings.data_dir
TABLE = DATA / "bus_routes.json"
COORDS = DATA / "bus_coord_regions.json"
MOLIT_CACHE = Path(__file__).resolve().parents[1] / "data" / "bus_molit"
SEOUL_BUS = "http://ws.bus.go.kr/api/rest/busRouteInfo"
KAKAO = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
KAKAO_ADDR = "https://dapi.kakao.com/v2/local/search/address.json"
MOLIT = "https://apis.data.go.kr/1613000/BusRoutespecificStopInformation/getBusRoutespecificStopInformation"
_MOLIT_CTPV = {"11", "12", "26", "27", "28", "29", "30", "31", "36", "41", "43", "44", "46", "47", "48", "50", "51",
               "52"}  # 12 = 전남광주통합특별시(2026-07~)
# 2026-07 개편(인천 구 개편·전남광주 통합) 전 코드 — 카카오는 새 코드만 줘서, 개편 전 운행일자를 쓸 때만 직접 적은 옛 코드로.
# 틀린 코드는 '자료 없음'으로만 끝나고, 지역 이름은 응답에 실린 이름을 쓰므로 잘못 붙을 일은 없음.
_OLD_SGG = {
    "28": "28110 28140 28177 28185 28200 28237 28245 28260 28710 28720",
    "29": "29110 29140 29155 29170 29200",
    "46": "46110 46130 46150 46170 46230 46710 46720 46730 46770 46780 46790 46800 46810 46820 46830 46840 46860 "
          "46870 46880 46890 46900 46910",
}

# 서울 버스 API routeType → 종류. 7 인천·8 경기는 서울에 들어오는 노선(이름 끝에 도시: '320안산'). 9 폐지는 뺌.
SEOUL_KIND = {"1": "공항", "2": "마을", "3": "간선", "4": "지선", "5": "순환", "6": "광역", "7": "인천", "8": "경기",
              "10": "투어", "15": "심야"}
_SIDO_SHORT = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원특별자치도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라남도": "전남", "경상북도": "경북",
    "경상남도": "경남", "제주특별자치도": "제주",
}
# 인천 행정구역 개편(2026-07): 카카오는 새 이름, 국토부 자료(6월까지)는 옛 이름, 습득물 색인엔 둘이 섞여 있음 → 둘 다 넣음
_INCHEON_OLD = {"영종구": ("중구",), "제물포구": ("중구", "동구"), "서해구": ("서구",), "검단구": ("서구",)}
_INCHEON_NEW = {"중구": ("영종구", "제물포구"), "동구": ("제물포구",), "서구": ("서해구", "검단구")}
_METRO = {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종"}
_NOTE = ("노선번호 → 실제로 지나는 시군구. scripts/build_bus_routes.py가 만듦(직접 고치지 말 것). "
         "출처 seoul = 서울 버스노선 API(서울 시내 + 서울에 들어오는 경기·인천 버스), "
         "tago = TAGO 버스노선정보(대구·광주 시내버스 — 국토부 자료에 없음), "
         "molit = 국토교통부 버스노선별 경유정류장(전국, 서울·TAGO 출처에 있는 노선은 뺌).")
_TAGS = re.compile(r"\(.*?\)|출근|퇴근|예약|급행하행|급행|거점")


class ApiStop(Exception):
    """공공 API가 거절(한도 초과·키 오류 등) — 지금까지 받은 것만 저장하고 멈춤."""


class ApiFlaky(ApiStop):
    """제공 서버가 몇 번 다시 물어도 오류(국토부 04 HTTP_ERROR 등) — 그 시군구만 건너뛰고 다음 실행에 다시."""


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
    if sido == "인천광역시":
        out += [f"인천 {o}" for o in _INCHEON_OLD.get(sgg, _INCHEON_NEW.get(sgg, ()))]
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
    table["_note"] = _NOTE
    _save(TABLE, table)
    done = sum(1 for k in routes_out if k.startswith("seoul:"))
    print(f"노선표 저장: 서울 출처 {done}/{len(listed)}개" + (f" — 미완료({stopped})" if stopped else ""))


# ---------- 전국: 국토교통부 버스노선별 경유정류장 ----------
def molit_page(sgg: str, opr_ymd: str, page: int = 1, rows: int = 1000) -> tuple[int, list[dict]]:
    """한 시군구의 정류장 행(그 시군구 안 정류장만). (전체 건수, 행들). 자료 없음은 (0, [])."""
    q = urllib.parse.urlencode({"serviceKey": settings.data_go_kr_service_key_decoded, "dataType": "JSON",
                                "pageNo": page, "numOfRows": rows, "opr_ymd": opr_ymd,
                                "ctpv_cd": sgg[:2], "sgg_cd": sgg})
    last = ""
    for i in range(5):
        if i:
            time.sleep(5 * 2 ** i)  # 10·20·40·80초
        try:
            with urllib.request.urlopen(f"{MOLIT}?{q}", timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ApiStop(f"국토부 HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"네트워크 {e}"
            continue
        try:
            j = json.loads(body)
        except ValueError:
            raise ApiStop(f"국토부 응답: {re.sub(r'<[^>]+>', ' ', body)[:120].strip()}") from None
        if "Error" in j:
            if j["Error"].get("code") == "50":  # NO_DATA_FOUND
                return 0, []
            raise ApiStop(f"국토부 오류 {j['Error']}")
        if "OpenAPI_ServiceResponse" in j:  # 포털 관문 오류 — 04 HTTP_ERROR·05 시간초과·99는 제공 서버 쪽이라 잠시 뒤 다시
            h = j["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            last = f"{h.get('returnReasonCode')} {h.get('errMsg')}"
            if h.get("returnReasonCode") in ("04", "05", "99"):
                continue
            raise ApiStop(f"국토부 관문 오류 {last}")  # 22 한도 초과·30 키 미등록 등
        b = (j.get("Response") or {}).get("body")
        if b is None:
            raise ApiStop(f"국토부 응답 모양 다름: {body[:120]}")
        items = (b.get("items") or {}).get("item") or []
        return int(b.get("totalCount") or 0), items if isinstance(items, list) else [items]
    raise ApiFlaky(f"국토부 계속 실패({last})")


def molit_latest_date(today: date | None = None) -> str:
    """자료가 있는 가장 최근 운행일자(YYYYMMDD). 몇 달 늦게 올라오므로 주 단위로 거슬러 찾은 뒤 하루 단위로 당김."""
    d = today or date.today()
    probe = "11110"  # 서울 종로구 — 노선이 늘 있음
    for _ in range(60):
        if molit_page(probe, d.strftime("%Y%m%d"), rows=1)[0]:
            break
        d -= timedelta(days=7)
    else:
        raise ApiStop("국토부 자료가 1년 넘게 없음")
    for _ in range(6):
        nxt = d + timedelta(days=1)
        if nxt > (today or date.today()) or not molit_page(probe, nxt.strftime("%Y%m%d"), rows=1)[0]:
            break
        d = nxt
    return d.strftime("%Y%m%d")


_SIDO_FULL = {**{v: k for k, v in _SIDO_SHORT.items()}, **{k: k for k in _SIDO_SHORT},
              "강원도": "강원특별자치도", "전라북도": "전북특별자치도", "서울시": "서울특별시"}


def kakao_sgg_code(name: str) -> str:
    """'경기도 고양시 덕양구' → '41281'(카카오 주소 검색의 법정동 코드 앞 5자리). 실패면 ''."""
    url = f"{KAKAO_ADDR}?{urllib.parse.urlencode({'query': name})}"
    req = urllib.request.Request(url, headers={"Authorization": f"KakaoAK {settings.kakao_rest_api_key}"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                docs = json.loads(r.read()).get("documents", [])
            return next(((d.get("address") or {}).get("b_code", "")[:5] for d in docs
                         if d.get("address_type") == "REGION" and (d.get("address") or {}).get("b_code")), "")
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1 + i)
    return ""


def sgg_candidates(opr_ymd: str = "") -> list[str]:
    """전국 시군구 코드 후보. 보관장소 사전(경찰서·지구대 주소, 전국 시군구가 다 있음)의 '시도 시군구(구)'를 카카오로 코드화
    (카카오는 지금 코드) + 운행일자가 2026-07 개편 전이면 옛 코드(_OLD_SGG). 시 아래 일반구(고양시 → 덕양·일산동·일산서)는
    build_molit이 '자료 없음'일 때 더 찾음."""
    gaz = json.loads((DATA / "dep_gazetteer.json").read_text(encoding="utf-8"))
    names: set[str] = set()
    for v in gaz["stations"].values():
        for src in (v.get("region", ""), v.get("addr", "")):
            t = src.split()
            if len(t) < 2 or t[0] not in _SIDO_FULL:
                continue
            names.add(f"{_SIDO_FULL[t[0]]} {t[1]}")
            if len(t) >= 3 and t[1].endswith("시") and t[2].endswith("구"):
                names.add(f"{_SIDO_FULL[t[0]]} {t[1]} {t[2]}")
    names = {n for n in names if n.split()[1][-1] in "시군구"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = set(ex.map(kakao_sgg_code, sorted(names)))
    codes = {c for c in codes if len(c) == 5 and c[:2] in _MOLIT_CTPV}
    if opr_ymd and opr_ymd < "20260701":
        codes = {c for c in codes if c[:2] not in _OLD_SGG and c[:2] != "12"}
        codes |= {c for v in _OLD_SGG.values() for c in v.split()}
    codes.add("36110")  # 세종 — 시군구가 없어 사전 주소에서 안 나옴
    if opr_ymd >= "20260701" or not opr_ymd:
        codes.add("28275")  # 인천 서해구 — 사전의 옛 '인천 서구'를 카카오는 검단구(28290)로 돌려줘 빠졌음(11/18)
    return sorted(codes)


def _molit_sgg(sgg: str, opr_ymd: str) -> dict | None:
    """한 시군구 → {'label': '경기도 고양시 덕양구', 'routes': {'노선ID|번호': {no, nm, min, max, n, first, last}}}. 자료 없으면 None."""
    total, rows = molit_page(sgg, opr_ymd)
    if not total:
        return None
    for p in range(2, (total + 999) // 1000 + 1):
        rows += molit_page(sgg, opr_ymd, page=p)[1]
    routes: dict[str, dict] = {}
    label = ""
    for r in rows:
        if not label:  # 세종처럼 시군구가 없으면 sgg_nm이 시도와 같거나 비어 있음
            sido, sgg_nm = r.get("ctpv_nm", ""), r.get("sgg_nm", "")
            label = sido if sgg_nm in ("", sido) else f"{sido} {sgg_nm}"
        try:
            seq = int(r.get("sttn_seq") or 0)
        except ValueError:
            continue
        k = f"{r.get('rte_id', '')}|{r.get('rte_no', '')}"
        a = routes.get(k)
        if a is None:
            routes[k] = {"no": r.get("rte_no", ""), "nm": r.get("rte_nm", ""), "min": seq, "max": seq, "n": 1,
                         "first": r.get("sttn_nm", ""), "last": r.get("sttn_nm", "")}
            continue
        a["n"] += 1
        if seq < a["min"]:
            a["min"], a["first"] = seq, r.get("sttn_nm", "")
        if seq > a["max"]:
            a["max"], a["last"] = seq, r.get("sttn_nm", "")
    return {"label": label, "routes": routes}


def molit_key(no: str | None) -> str:
    """'1500(예약)' → '1500', '15-1(구파발)' → '15-1', '171번' → '171', '급행1' → '급행1'(급행·좌석·마을은 노선 이름의 일부).
    번호 없는 노선(시외·공항 리무진 '창원-여수'·'서면부전-김해공항', 11/18 실측 683개 구간)은 '' → 표에서 뺌(번호로 못 찾음)."""
    k = re.sub(r"\(.*?\)|\s+", "", no or "")
    return re.sub(r"번$", "", k).upper()


def _home_city(label: str) -> str:
    """정류장이 가장 많은 시군구 → 도시 이름. 특별·광역시는 시도('부산'), 도는 시군('고양시 덕양구' → '고양')."""
    names = region_names(label)
    if not names:
        return ""
    t = names[0].split()  # '부산 중구'·'경기 고양시'·'광주 서구'(전남광주통합특별시도 옛 시도로)·'세종'
    if t[0] in _METRO or len(t) < 2:
        return t[0]
    return re.sub(r"(시|군)$", "", t[1]) or t[0]


def molit_routes(sggs: dict[str, dict | None], seoul: dict) -> tuple[dict, Counter]:
    """시군구별 결과 → 노선별 표 항목. 서울·TAGO 출처(seoul 인자)에 같은 번호·같은 구간(구가 절반 이상 겹침)이 있으면 뺌."""
    parts: dict[str, list] = {}
    for got in sggs.values():
        if not got:
            continue
        for k, a in got["routes"].items():
            parts.setdefault(k, []).append((a, got["label"]))
    seoul_by_key: dict[str, list[list[str]]] = {}
    for r in seoul.values():
        seoul_by_key.setdefault(r["key"], []).append(r["regions"])
    out: dict = {}
    stat: Counter = Counter()
    for k, ps in parts.items():
        ps.sort(key=lambda p: p[0]["min"])
        regs: list[str] = []
        for _, label in ps:
            for name in region_names(label):
                if name not in regs:
                    regs.append(name)
        if not regs:
            continue
        key = molit_key(ps[0][0]["no"])
        if not key:
            continue
        if _same_route(regs, seoul_by_key.get(key, [])):
            stat["서울·TAGO 출처와 같음(뺌)"] += 1
            continue
        home = max(ps, key=lambda p: p[0]["n"])[1]
        last = max(ps, key=lambda p: p[0]["max"])[0]
        rid = k.split("|")[0]
        out[f"molit:{rid}:{key}"] = {
            "name": ps[0][0]["no"], "key": key, "city": _home_city(home), "regions": regs,
            "start_region": region_names(ps[0][1])[0], "start": ps[0][0]["first"], "end": last["last"],
        }
        stat[region_names(home)[0].split()[0]] += 1
    return out, stat


def build_molit(force: bool = False, opr_ymd: str = "") -> None:
    MOLIT_CACHE.mkdir(parents=True, exist_ok=True)
    if not opr_ymd and not force:  # 열흘 안에 시작한 수집이 있으면 그 운행일자로 이어 받음(날짜 찾기 호출 아낌)
        recent = sorted(f for f in MOLIT_CACHE.glob("*.json")
                        if time.time() - f.stat().st_ctime < 10 * 86400)
        opr_ymd = recent[-1].stem if recent else ""
    opr_ymd = opr_ymd or molit_latest_date()
    cache_path = MOLIT_CACHE / f"{opr_ymd}.json"
    cache = {} if force else _load(cache_path, {})
    sggs: dict[str, dict | None] = cache.setdefault("sgg", {})
    cache["candidates"] = sgg_candidates(opr_ymd)  # 카카오만 부름(국토부 호출 아님) — 매번 지금 코드로
    todo = [c for c in cache["candidates"] if c not in sggs]
    print(f"국토부 운행일자 {opr_ymd}: 시군구 후보 {len(cache['candidates'])}개, 받을 것 {len(todo)}개")
    lock = threading.Lock()
    stopped: list[str] = []
    flaky: list[str] = []

    def one(code: str) -> None:
        if stopped:
            return
        try:
            got = _molit_sgg(code, opr_ymd)
            # 시 단위 코드가 비면 그 아래 일반구(41280 고양시 → 41281·41285·41287)
            subs = {}
            if got is None and code.endswith("0"):
                for d in "123456789":
                    sub = code[:4] + d
                    if sub not in sggs:
                        subs[sub] = _molit_sgg(sub, opr_ymd)
        except ApiFlaky:
            flaky.append(code)
            return
        except ApiStop as e:
            stopped.append(str(e))
            return
        with lock:
            sggs[code] = got
            sggs.update({s: g for s, g in subs.items() if g})
            done = len(sggs)
            if done % 10 == 0:
                _save(cache_path, cache)
                print(f"  시군구 {done}개")

    with ThreadPoolExecutor(max_workers=2) as ex:  # 4개씩 동시에 큰 페이지를 물었을 때 제공 서버가 04 오류로 멈췄음
        list(ex.map(one, todo))
    _save(cache_path, cache)
    if stopped or flaky:
        why = stopped[0] if stopped else f"제공 서버 오류로 {len(flaky)}개 건너뜀"
        print(f"  미완료: {why} — 다시 실행하면 남은 시군구만 받음(표는 다 받은 뒤에 바꿈)")
        return

    table = _load(TABLE, {"routes": {}})
    keep = {k: v for k, v in table["routes"].items() if k.startswith(("seoul:", "tago:"))}  # 서울·TAGO 출처는 그대로
    routes, stat = molit_routes(sggs, keep)
    table["routes"] = {**keep, **routes}
    table.setdefault("sources", {})["molit"] = {"opr_ymd": opr_ymd, "fetched": date.today().isoformat(),
                                               "sgg_with_data": sum(1 for g in sggs.values() if g)}
    table["_note"] = _NOTE
    _save(TABLE, table)
    print(f"노선표 저장: 전국 출처 {len(routes)}개 (자료 있는 시군구 {table['sources']['molit']['sgg_with_data']}개)")
    print("  " + ", ".join(f"{k} {v}" for k, v in stat.most_common()))


# ---------- 대구·광주 시내버스: TAGO(국토교통부 대중교통 정보 — 버스노선정보) ----------
# 국토부 경유정류장 자료엔 대구·광주 시내버스가 거의 없다(11/18 실측: 대구 0개, 광주는 마을버스·화순 노선뿐 — 두 도시는
# 자체 BIS로 운영). 같은 공공데이터포털 키의 TAGO로 채움: 도시별 노선 목록 → 노선별 정류장(좌표, 순서) → 카카오로 구.
TAGO = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService"
TAGO_CACHE = Path(__file__).resolve().parents[1] / "data" / "bus_tago"
TAGO_CITIES = {"22": "대구", "24": "광주"}  # TAGO 도시코드(getCtyCodeList) → 도시


def tago(op: str, **params) -> tuple[int, list[dict]]:
    """TAGO 호출 → (전체 건수, item 목록). 결과 없음은 (0, []). 포털 관문 04·05·99는 잠시 뒤 다시, 한도·키 오류는 멈춤."""
    q = urllib.parse.urlencode({**params, "serviceKey": settings.data_go_kr_service_key_decoded, "_type": "json"})
    last = ""
    for i in range(5):
        if i:
            time.sleep(3 * 2 ** i)  # 6·12·24·48초
        try:
            with urllib.request.urlopen(f"{TAGO}/{op}?{q}", timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ApiStop(f"TAGO HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"네트워크 {e}"
            continue
        gate = re.search(r"returnReasonCode\W+(\d+)", body)  # 관문 오류는 JSON을 달라 해도 XML로 오기도 함
        if gate and "response" not in body[:40]:
            last = f"관문 {gate.group(1)} {re.sub(r'<[^>]+>', ' ', body)[:80].strip()}"
            if gate.group(1) in ("04", "05", "99"):
                continue
            raise ApiStop(f"TAGO {last}")  # 22 한도 초과·30 키 미등록 등
        try:
            j = json.loads(body)
        except ValueError:
            raise ApiStop(f"TAGO 응답: {re.sub(r'<[^>]+>', ' ', body)[:120].strip()}") from None
        resp = j.get("response") or {}
        code = str((resp.get("header") or {}).get("resultCode", ""))
        if code == "03":  # NODATA
            return 0, []
        if code != "00":
            raise ApiStop(f"TAGO 오류 {resp.get('header')}")
        b = resp.get("body") or {}
        items = (b.get("items") or {}).get("item") or [] if isinstance(b.get("items"), dict) else []
        return int(b.get("totalCount") or 0), items if isinstance(items, list) else [items]
    raise ApiFlaky(f"TAGO 계속 실패({last})")


def _tago_all(op: str, rows: int, **params) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        total, items = tago(op, numOfRows=rows, pageNo=page, **params)
        out += items
        if page * rows >= total or not items:
            return out
        page += 1


def _same_route(regs: list[str], others: list[list[str]]) -> bool:
    """같은 번호의 다른 출처 노선과 구가 절반 이상 겹치면 같은 노선으로 봄(고양 720 ≠ 서울 720처럼 다른 노선은 남김)."""
    return any(sum(region_matches(g, s) for g in regs) * 2 >= len(regs) for s in others)


def tago_routes(cache: dict, coords: dict) -> dict:
    """받아 둔 노선별 정류장(순서대로 좌표) → 표 항목. 구는 노선 순서대로."""
    out: dict = {}
    for rid, r in cache.items():
        regs: list[str] = []
        for k in r["stops"]:
            for name in region_names(coords.get(k, "")):
                if name not in regs:
                    regs.append(name)
        key = molit_key(r["no"])
        if not regs or not key:
            continue
        out[f"tago:{r['city']}:{rid}"] = {
            "name": r["no"], "key": key, "city": TAGO_CITIES.get(r["city"], ""), "kind": r.get("tp", ""),
            "regions": regs, "start_region": (region_names(coords.get(r["stops"][0], "")) or [""])[0],
            "start": r.get("start", ""), "end": r.get("end", ""),
        }
    return out


def build_tago(force: bool = False, max_age_days: int = 25) -> None:
    TAGO_CACHE.mkdir(parents=True, exist_ok=True)
    path = TAGO_CACHE / "routes.json"
    cache: dict = {} if force else _load(path, {})
    fresh_after = (date.today() - timedelta(days=max_age_days)).isoformat()
    listed: dict[str, dict] = {}
    for city in TAGO_CITIES:
        for it in _tago_all("getRouteNoList", 500, cityCode=city):
            listed[str(it["routeid"])] = {**it, "city": city}
    for rid in [k for k in cache if k not in listed]:  # 폐지·번호 바뀐 노선
        del cache[rid]
    todo = [rid for rid in listed if force or cache.get(rid, {}).get("fetched", "") < fresh_after]
    print(f"TAGO 노선 {len(listed)}개({', '.join(TAGO_CITIES.values())}), 정류장 받을 노선 {len(todo)}개")
    stopped = ""
    for i, rid in enumerate(todo, 1):
        it = listed[rid]
        try:
            st = _tago_all("getRouteAcctoThrghSttnList", 1000, cityCode=it["city"], routeId=rid)
        except ApiStop as e:  # ApiFlaky 포함 — 받은 데까지 저장, 다음 실행이 이어서
            stopped = str(e)
            break
        st.sort(key=lambda s: int(s.get("nodeord") or 0))
        stops = [k for s in st if (k := _coord_key(str(s.get("gpslong", "")), str(s.get("gpslati", ""))))]
        cache[rid] = {"city": it["city"], "no": str(it.get("routeno", "")), "tp": it.get("routetp", ""),
                      "start": it.get("startnodenm", ""), "end": it.get("endnodenm", ""), "stops": stops,
                      "fetched": date.today().isoformat()}
        if i % 50 == 0:
            _save(path, cache)
            print(f"  정류장 {i}/{len(todo)}")
    _save(path, cache)
    if stopped or len(cache) < len(listed):
        print(f"  미완료: {stopped or '덜 받음'} — 다시 실행하면 남은 노선만 받음(표는 다 받은 뒤에 바꿈)")
        return

    coords: dict = _load(COORDS, {})
    need = sorted({k for r in cache.values() for k in r["stops"] if k not in coords})
    print(f"카카오 좌표 조회 {len(need)}개 (캐시 {len(coords)}개)")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, reg in zip(need, ex.map(lambda k: kakao_region(*map(float, k.split(","))), need), strict=True):
            if reg:
                coords[k] = reg
    _save(COORDS, coords)

    routes = tago_routes(cache, coords)
    table = _load(TABLE, {"routes": {}})
    by_key: dict[str, list[list[str]]] = {}
    for r in routes.values():
        by_key.setdefault(r["key"], []).append(r["regions"])
    kept = {k: v for k, v in table["routes"].items() if not k.startswith("tago:")}
    dup = [k for k, v in kept.items() if k.startswith("molit:") and _same_route(v["regions"], by_key.get(v["key"], []))]
    table["routes"] = {**{k: v for k, v in kept.items() if k not in dup}, **routes}
    table.setdefault("sources", {})["tago"] = {"cities": list(TAGO_CITIES.values()), "fetched": date.today().isoformat()}
    table["_note"] = _NOTE
    _save(TABLE, table)
    stat = Counter(r["city"] for r in routes.values())
    print(f"노선표 저장: TAGO 출처 {len(routes)}개 ({', '.join(f'{k} {v}' for k, v in stat.items())}),"
          f" 같은 노선이라 뺀 국토부 출처 {len(dup)}개")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="받은 날짜와 상관없이 전부 다시")
    ap.add_argument("--max-age-days", type=int, default=25)
    ap.add_argument("--only", choices=["seoul", "molit", "tago"])
    ap.add_argument("--date", default="", help="국토부 운행일자 YYYYMMDD(기본: 이어 받던 날짜, 없으면 자료 있는 최근 날짜)")
    a = ap.parse_args()
    try:
        if a.only in (None, "seoul"):
            build_seoul(force=a.force, max_age_days=a.max_age_days)
        if a.only in (None, "molit"):
            build_molit(force=a.force, opr_ymd=a.date)
        if a.only in (None, "tago"):
            build_tago(force=a.force, max_age_days=a.max_age_days)
    except ApiStop as e:
        print(f"멈춤: {e} — 받은 데까지는 저장됨, 나중에 다시 실행하면 이어 받음")


if __name__ == "__main__":
    main()
