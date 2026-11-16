"""실제 분실 신고로 재는 실측 — 경찰청 분실물정보(LOST112)에 올라온 분실 신고를 우리 검색에 그대로 넣어 본다.

제한된 문항(직접 만든 질문)이 아니라 실제 주인이 쓴 신고로, "이미 보관 중인 습득물을 우리가 찾아 주는가"를 잰다.

  uv run python -m eval.run_lost112 fetch                 # 분실 신고 목록 받기(오늘 날짜 스냅숏 — 매일 돌리면 사라진 신고 추적)
  uv run python -m eval.run_lost112 sample --n 300        # 분류 비례(최소 5) 무작위 표본 + 상세 받기
  uv run python -m eval.run_lost112 search                # 실제 서비스 흐름(추출→지역→검색→판정 8개) — OPENSEARCH_URL 필요
  uv run python -m eval.run_lost112 judge                 # 상위 8개를 다른 모델로 엄격 판정(강함/가능/없음)
  uv run python -m eval.run_lost112 report                # 집계(eval/results/lost112.md) + 지켜볼 목록 + 운영자 확인 30건
  PROD_SSH=root@서버 uv run python -m eval.run_lost112 watch   # 그 뒤 후보가 종결됐는지(운영 종결 기록 읽기만)
  uv run python -m eval.run_lost112 lang --n 100          # 같은 신고를 영·중·일로(AI 번역) 넣어 같은 습득물을 찾는지

- 원자료(신고·후보·판정)는 data/lost112/에만 — 저장소에 안 올림(.gitignore). 저장소엔 이 스크립트와 집계만.
- 신고의 특이사항은 API가 전부 가림("개인정보보호정책…") → 실제 사용자보다 정보가 적은 어려운 조건.
- 판정은 AI(서비스 판정과 다른 모델)라 정답이 아님 — 운영자 대조(review.md)로 믿을 만한지 따로 확인.
- 색인은 읽기만. 신고·후보를 저장하지 않음(graph.run은 저장 없이 결과만 돌려줌).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import random
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.api.config import settings

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "data" / "lost112"
OUT = Path(__file__).resolve().parent / "results"
API = "https://apis.data.go.kr/1320000/LostGoodsInfoInqireService"
START, END = "20261020", "20261112"  # 분실일 범위 — 신고 뒤 습득물이 올라올 시간을 둠(습득물 보관 ~185일 안)
JUDGE_MODEL = "gemini-3.8-flash"     # 서비스 판정(3.6-flash)과 다른 모델로 — 같은 모델이 제 답을 채점하지 않게
HIDDEN = "개인정보보호정책"


# ── 받기 ─────────────────────────────────────────────────────────────
def _call(op: str, **params) -> ET.Element:
    params["serviceKey"] = settings.data_go_kr_service_key_decoded
    url = f"{API}/{op}?{urllib.parse.urlencode(params)}"
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                root = ET.fromstring(r.read())
            code = root.findtext(".//resultCode")
            if code not in (None, "00"):
                raise RuntimeError(f"{op} {code} {root.findtext('.//resultMsg')}")
            return root
        except (OSError, ET.ParseError):
            if i == 3:
                raise
            time.sleep(2 * (i + 1))
    raise RuntimeError("unreachable")


def _items(root: ET.Element) -> list[dict]:
    return [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]


def fetch() -> Path:
    first = _call("getLostGoodsInfoAccToClAreaPd", pageNo=1, numOfRows=1000, START_YMD=START, END_YMD=END)
    total = int(first.findtext(".//totalCount") or 0)
    rows = _items(first)
    for page in range(2, -(-total // 1000) + 1):
        rows += _items(_call("getLostGoodsInfoAccToClAreaPd", pageNo=page, numOfRows=1000,
                             START_YMD=START, END_YMD=END))
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / f"list_{dt.date.today():%Y%m%d}.jsonl"
    seen: set[str] = set()
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            if r.get("atcId") and r["atcId"] not in seen:
                seen.add(r["atcId"])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{out.name}: 전체 {total} / 받은 {len(seen)}")
    return out


def _load(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _save(p: Path, rows: list[dict]) -> None:
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def sample(n: int, seed: int) -> None:
    lists = sorted(RAW.glob("list_*.jsonl"))
    rows = _load(lists[-1] if lists else fetch())
    by_top: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_top[(r.get("prdtClNm") or "기타").split(" > ")[0].strip()].append(r)
    rng = random.Random(seed)
    # 분류 비례 배분 + 최소 5(작은 분류도 보이게) — 합이 n이 되도록 큰 분류에서 덜어냄
    quota = {k: max(5, round(n * len(v) / len(rows))) for k, v in by_top.items()}
    while sum(quota.values()) > n:
        big = max(quota, key=quota.get)
        quota[big] -= 1
    picked: list[dict] = []
    for k, v in sorted(by_top.items()):
        picked += rng.sample(v, min(quota[k], len(v)))
    with ThreadPoolExecutor(max_workers=4) as ex:
        details = list(ex.map(lambda r: (_items(_call("getLostGoodsDetailInfo", ATC_ID=r["atcId"])) or [{}])[0], picked))
    out = []
    for r, d in zip(picked, details, strict=True):
        m = {**r, **{k: v for k, v in d.items() if v}}
        if HIDDEN in m.get("uniq", ""):
            m["uniq"] = ""
        if "no_img" in m.get("lstFilePathImg", ""):  # 사진 없음 자리표시 그림
            m["lstFilePathImg"] = ""
        out.append(m)
    _save(RAW / "sample.jsonl", out)
    print(f"표본 {len(out)}건 — " + ", ".join(f"{k} {quota[k]}" for k in sorted(quota, key=lambda k: -quota[k])))


# ── 검색 ─────────────────────────────────────────────────────────────
def to_text(r: dict) -> str:
    """주인이 적은 칸으로 한 문장 — 실제 사용자가 쓰는 말투에 가깝게. 특이사항은 API가 가려서 없음."""
    when = r.get("lstYmd", "")
    if r.get("lstHor", "").isdigit():
        when += f" {int(r['lstHor'])}시쯤"
    where = " ".join(x for x in (r.get("lstLctNm", ""), r.get("lstPlace", "")) if x)
    if r.get("lstPlaceSeNm") and r["lstPlaceSeNm"] not in where:
        where += f"({r['lstPlaceSeNm']})"
    color = (r.get("clrNm") or "").split("(")[0]
    item = r.get("lstPrdtNm", "")
    what = f"{color} {item}".strip() if color and color not in item else item
    s = f"{when} {where}에서 {what}{_eul(what)} 잃어버렸어요. {r.get('lstSbjt', '')}".strip()
    return s if not r.get("uniq") else f"{s} {r['uniq']}"


def _eul(word: str) -> str:
    """을/를 — 마지막 한글의 받침으로. 한글이 아니면(괄호·영문 끝) '을(를)'."""
    w = word.rstrip(") ").rstrip()
    ch = w[-1] if w else ""
    if "가" <= ch <= "힣":
        return "을" if (ord(ch) - 0xAC00) % 28 else "를"
    return "을(를)"


_KEEP = ("atc_id", "fd_sn", "name", "category", "color", "description", "dep_place", "found_place",
         "region", "found_at", "org_name", "image_url", "score", "grade", "reason")


def _search_one(r: dict) -> dict:
    from apps.agent import graph
    from apps.api.pii import mask

    text, _ = mask(to_text(r))
    t0 = time.time()
    try:
        st = graph.run(text, lost_date=r.get("lstYmd"), lang="ko")
    except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않게
        return {"atcId": r["atcId"], "text": text, "error": f"{type(e).__name__}: {e}"[:300]}
    ex = st.get("extracted") or {}
    return {
        "atcId": r["atcId"], "text": text, "secs": round(time.time() - t0, 1),
        "is_lost_report": ex.get("is_lost_report", True),
        "extracted": {k: ex.get(k) for k in ("item", "color", "brand", "category", "place_context", "lost_date")},
        "region_set": st.get("region_set") or [],
        "matches": [{k: (m.get(k) or "")[:300] if isinstance(m.get(k), str) else m.get(k) for k in _KEEP}
                    for m in (st.get("matches") or [])],
    }


TOKENS = {"calls": 0, "in": 0, "out": 0}


def _count_tokens() -> None:
    """AI 토큰 실측(비용 확인) — 로컬 통계 DB엔 안 씀."""
    from apps.api import gemini

    def meter(res):
        u = res.get("usageMetadata") if isinstance(res, dict) else None
        if u:
            TOKENS["calls"] += 1
            TOKENS["in"] += u.get("promptTokenCount", 0)
            TOKENS["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
    gemini._meter = meter


def search(workers: int) -> None:
    _count_tokens()
    rows = _load(RAW / "sample.jsonl")
    out_p = RAW / "search.jsonl"
    done = {x["atcId"]: x for x in _load(out_p)} if out_p.exists() else {}
    todo = [r for r in rows if r["atcId"] not in done or done[r["atcId"]].get("error")]
    print(f"검색 {len(todo)}건(이미 {len(rows) - len(todo)}) — 색인 {settings.opensearch_url}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(_search_one, todo), 1):
            done[res["atcId"]] = res
            if i % 20 == 0 or i == len(todo):
                _save(out_p, [done[r["atcId"]] for r in rows if r["atcId"] in done])
                print(f"  {i}/{len(todo)}")
    errs = sum(1 for x in done.values() if x.get("error"))
    print(f"끝 — 오류 {errs}, AI 호출 {TOKENS}")


# ── 판정 ─────────────────────────────────────────────────────────────
JUDGE_PROMPT = """너는 분실물 대조 심사관이다. 아래 '분실 신고'의 물건과 각 '습득물'이 **같은 실물**인지 엄격하게 판정하라.
서비스가 매긴 점수는 주지 않는다. 오직 두 기록의 사실만 보고 판단하라.

[분실 신고] (주인이 경찰청에 낸 신고. 특이사항은 공개되지 않아 없음)
{lost}

[습득물 후보]
{cands}

판정 기준:
- "strong": 같은 종류이고, 색·브랜드·모델·무늬·캐릭터 같은 **구체적 특징이 하나 이상 서로 맞고** 어긋나는 점이 없으며,
  습득일이 분실일과 같거나 뒤(30일 안)이고, 장소가 이어질 만함(같은 노선·역·건물·동네, 또는 그곳을 맡는 기관).
  흔한 물건(검정 지갑, 흰 이어폰 등)은 장소·날짜까지 잘 맞아야 strong.
- "possible": 같은 종류이고 어긋나는 점은 없지만, 정보가 흔하거나 부족해 같은 물건이라 말하기 어려움.
- "none": 종류가 다르거나, 색·브랜드 등이 어긋나거나, 습득일이 분실일보다 앞서거나, 장소가 이어질 근거가 없음.
습득물 설명에 다른 사람 이름·내용물이 적혀 있어도 신고와 맞춰 볼 근거가 없으면 판단에 쓰지 마라.

JSON으로만: {{"verdicts": [{{"id": 후보번호, "v": "strong|possible|none", "why": "20자 안팎 한국어"}}]}}
"""


def _lost_block(r: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in (
        ("제목", r.get("lstSbjt")), ("물품", r.get("lstPrdtNm")), ("분류", r.get("prdtClNm")),
        ("색", r.get("clrNm")), ("분실 지역", r.get("lstLctNm")), ("분실 장소", r.get("lstPlace")),
        ("장소 구분", r.get("lstPlaceSeNm")), ("분실일", f"{r.get('lstYmd', '')} {r.get('lstHor', '')}시")) if v)


def _cand_block(ms: list[dict]) -> str:
    return "\n".join(
        f"#{i} 물품={m.get('name')} | 분류={m.get('category')} | 색={m.get('color')} | 습득일={m.get('found_at')} | "
        f"습득장소={m.get('found_place')} | 보관={m.get('dep_place')}({m.get('region')}) | 특이사항={_desc(m) or '없음'}"
        for i, m in enumerate(ms))


def _judge_one(pair: tuple[dict, dict]) -> dict:
    from apps.api import gemini

    r, s = pair
    ms = s.get("matches") or []
    if not ms:
        return {"atcId": r["atcId"], "verdicts": []}
    prompt = JUDGE_PROMPT.format(lost=_lost_block(r), cands=_cand_block(ms))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_MODEL}:generateContent"
    for attempt in range(3):
        try:
            res = gemini._post(url, body, timeout=90)
            data = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
            vs = {int(v["id"]): v for v in data.get("verdicts", []) if str(v.get("id", "")).lstrip("#").isdigit()}
            return {"atcId": r["atcId"], "verdicts": [
                {"v": (vs.get(i) or {}).get("v", "none"), "why": (vs.get(i) or {}).get("why", "")} for i in range(len(ms))]}
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
            time.sleep(3 * (attempt + 1))
    return {"atcId": r["atcId"], "error": err}


def judge(workers: int) -> None:
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    srch = [s for s in _load(RAW / "search.jsonl") if not s.get("error")]
    out_p = RAW / "judge.jsonl"
    done = {x["atcId"]: x for x in _load(out_p)} if out_p.exists() else {}
    todo = [(rows[s["atcId"]], s) for s in srch if s["atcId"] not in done or done[s["atcId"]].get("error")]
    _count_tokens()
    print(f"판정 {len(todo)}건 — {JUDGE_MODEL}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(_judge_one, todo), 1):
            done[res["atcId"]] = res
            if i % 20 == 0 or i == len(todo):
                _save(out_p, list(done.values()))
                print(f"  {i}/{len(todo)}")
    print(f"끝 — 오류 {sum(1 for x in done.values() if x.get('error'))}, AI 호출 {TOKENS}")


RECHECK_PROMPT = """두 기록이 같은 실물인지 '보수적으로' 다시 본다. 1차 심사는 같은 물건으로 봤다.

[분실 신고]
{lost}

[습득물]
{cand}

1) 두 기록 **양쪽에 모두 적힌** 구체적 특징만 뽑아라: 브랜드·모델명·캐릭터·이름(가린 이름 포함)·여권 국적·
   통장 은행·특이한 물건 종류(예: '마운자로', '번호판') 등. 색·흔한 종류(지갑·가방·이어폰)·지역·날짜는 구체적 특징이 아니다.
2) 어긋나는 점(종류·브랜드·모델·색이 다름, 한쪽에만 있는 결정적 정보가 다른 쪽과 모순)을 뽑아라.
3) 판정: 구체적 특징이 하나 이상 서로 맞고 어긋남이 없으면 "confirmed", 아니면 "plausible"
   (종류·색·장소·날짜만 맞음), 어긋남이 있으면 "doubtful".

JSON으로만: {{"shared": ["..."], "conflicts": ["..."], "v": "confirmed|plausible|doubtful"}}
"""


def recheck(workers: int) -> None:
    """1차 strong 후보만 2차로 — '양쪽에 적힌 구체적 특징이 맞는가'로 보수적 하한(confirmed)을 따로 낸다."""
    from apps.api import gemini

    _count_tokens()
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    jobs = []
    for j in _load(RAW / "judge.jsonl"):
        for k, v in enumerate(j.get("verdicts") or []):
            if v["v"] == "strong":
                jobs.append((j["atcId"], k))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_MODEL}:generateContent"

    def one(job: tuple[str, int]) -> dict:
        i, k = job
        m = srch[i]["matches"][k]
        body = {"contents": [{"parts": [{"text": RECHECK_PROMPT.format(lost=_lost_block(rows[i]), cand=_cand_block([m]))}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        try:
            res = gemini._post(url, body, timeout=90)
            d = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
            return {"atcId": i, "idx": k, **{x: d.get(x) for x in ("shared", "conflicts", "v")}}
        except Exception as e:  # noqa: BLE001
            return {"atcId": i, "idx": k, "error": f"{type(e).__name__}: {e}"[:200]}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(one, jobs))
    _save(RAW / "recheck.jsonl", out)
    c = collections.Counter(x.get("v") or "error" for x in out)
    print(f"2차 {len(out)}개: {dict(c)} — AI 호출 {TOKENS}")


# ── 집계 ─────────────────────────────────────────────────────────────
def _desc(m: dict) -> str:
    """경찰 상투 문장을 걷어낸 특이사항 한 줄(화면·메일과 같은 기준)."""
    from apps.api.desc import clean_desc

    return " ".join((clean_desc(m.get("description")) or "").split())[:160]


def _first(vs: list[dict], want: set[str]) -> int | None:
    return next((i + 1 for i, v in enumerate(vs) if v.get("v") in want), None)


def report() -> None:
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl")}
    jud = {j["atcId"]: j for j in _load(RAW / "judge.jsonl")}
    ids = [i for i in rows if i in srch and not srch[i].get("error") and i in jud and not jud[i].get("error")]
    n = len(ids)
    strong = {i: _first(jud[i]["verdicts"], {"strong"}) for i in ids}
    poss = {i: _first(jud[i]["verdicts"], {"strong", "possible"}) for i in ids}
    rejected = sum(1 for i in ids if not srch[i].get("is_lost_report", True))
    empty = sum(1 for i in ids if not srch[i].get("matches"))

    def pct(a: int, b: int) -> str:
        return f"{a}건 ({a / b:.0%})" if b else "0건"

    s_any = [i for i in ids if strong[i]]
    rc_p = RAW / "recheck.jsonl"
    rc = [x for x in _load(rc_p) if not x.get("error")] if rc_p.exists() else []
    conf: dict[str, int] = {}  # 2차 confirmed가 있는 신고 → 가장 높은 순위
    for x in rc:
        if x.get("v") == "confirmed" and x["atcId"] in strong:
            conf[x["atcId"]] = min(conf.get(x["atcId"], 99), x["idx"] + 1)
    lines = [
        "# 실제 분실 신고 실측 (경찰청 LOST112)",
        "",
        f"- 측정: {dt.datetime.now():%Y-%m-%d %H:%M}, 분실일 {START}~{END} 신고 중 분류 비례 무작위 {n}건",
        "- 문장: 주인이 적은 제목·물품·색·장소·일시로 한 문장(특이사항은 API 비공개 → 실제 사용자보다 정보가 적음)",
        f"- 검색: 실제 서비스 흐름 그대로(추출→지역→검색→판정 {settings.grade_topk}개), 운영 색인 읽기만",
        f"- 판정: 서비스와 다른 모델({JUDGE_MODEL})이 상위 {settings.grade_topk}개를 신고와 1:1로 엄격 대조 — **정답이 아니라 AI 판정**",
        "",
        "## 전체",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 상위 {settings.grade_topk}개 안에 '같은 물건으로 보임(strong)'이 있는 신고 | {pct(len(s_any), n)} |",
        f"| └ 그중 1위 | {pct(sum(1 for i in s_any if strong[i] == 1), len(s_any))} |",
        f"| └ 그중 3위 안 | {pct(sum(1 for i in s_any if strong[i] <= 3), len(s_any))} |",
        f"| 보수적 하한: 2차에서 '양쪽에 적힌 구체적 특징(브랜드·모델·여권 국적 등)이 맞음' | {pct(len(conf), n)} |",
        f"| └ 그중 1위 | {pct(sum(1 for r in conf.values() if r == 1), len(conf))} |",
        f"| strong 또는 '가능(possible)'이 있는 신고 | {pct(sum(1 for i in ids if poss[i]), n)} |",
        f"| 신고로 안 받아준 문장 | {pct(rejected, n)} |",
        f"| 후보가 하나도 없음 | {pct(empty, n)} |",
        "",
        "## 분류별",
        "",
        "| 분류 | 신고 | strong 있음 | 1위 |",
        "|---|---|---|---|",
    ]
    cat = collections.defaultdict(list)
    for i in ids:
        cat[(rows[i].get("prdtClNm") or "기타").split(" > ")[0]].append(i)
    for k, v in sorted(cat.items(), key=lambda kv: -len(kv[1])):
        hit = [i for i in v if strong[i]]
        lines.append(f"| {k} | {len(v)} | {pct(len(hit), len(v))} | {sum(1 for i in hit if strong[i] == 1)} |")
    # 서비스 판정 점수와 AI 판정의 관계 — strong 후보를 서비스는 몇 점으로 봤나
    sg = [srch[i]["matches"][strong[i] - 1].get("grade") for i in s_any]
    sg = [g for g in sg if isinstance(g, (int, float))]
    if sg:
        lines += ["", f"서비스 판정 점수(strong 후보): 평균 {sum(sg) / len(sg):.2f}, 최저 {min(sg):.2f}"]
    secs = [srch[i].get("secs", 0) for i in ids]
    lines += ["", f"신고 1건 처리 시간: 중앙값 {sorted(secs)[len(secs) // 2]:.1f}초", "",
              "## 한계", "",
              "- AI 판정은 정답이 아님(운영자 대조로 믿을 만한지 확인).",
              "- 신고자 대부분 한국인 — 외국인 문장은 따로 잼.",
              "- 특이사항(내용물·흠집 등)이 가려져 있어 실제 사용자보다 어려운 조건.",
              "- '상위 8개 안에 없음'이 '보관 중인 습득물이 없음'은 아님(아직 안 들어왔거나, 우리가 못 찾았거나)."]
    OUT.mkdir(exist_ok=True)
    (OUT / "lost112.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 지켜볼 목록(종결되는지) — 모든 후보를 판정과 함께. 'none' 후보 = 같은 검색이 고른 비슷한 습득물(분류·지역·날짜가
    # 비슷함) → strong 후보의 종결률을 견줄 대조군
    watch = []
    for i in ids:
        for rank, (m, v) in enumerate(zip(srch[i]["matches"], jud[i]["verdicts"], strict=False), 1):
            watch.append({"lost": i, "key": f"{m['atc_id']}_{m['fd_sn']}", "v": v["v"], "rank": rank,
                          "category": m.get("category"), "found_at": m.get("found_at"), "region": m.get("region")})
    (RAW / "watch.json").write_text(json.dumps({"made": f"{dt.datetime.now():%Y-%m-%d %H:%M}", "items": watch},
                                               ensure_ascii=False, indent=1), encoding="utf-8")

    # 운영자 확인 30건 — strong 15 + possible만 10 + 없음 5(놓친 게 있는지)
    rng = random.Random(7)
    only_poss = [i for i in ids if poss[i] and not strong[i]]
    nothing = [i for i in ids if not poss[i] and srch[i].get("matches")]
    pick = (rng.sample(s_any, min(15, len(s_any))) + rng.sample(only_poss, min(10, len(only_poss)))
            + rng.sample(nothing, min(5, len(nothing))))
    md = ["# 운영자 확인 30건 — AI 판정이 맞는지", "",
          "각 건의 '내 판단' 칸에 같음 / 아마 / 다름 으로 적어 주세요. 사진 링크는 경찰청·포털 원본입니다.", ""]
    rc_v = {(x["atcId"], x["idx"]): x.get("v") for x in rc}
    for n_, i in enumerate(pick, 1):
        r, s, j = rows[i], srch[i], jud[i]
        rank = strong[i] or poss[i] or 1
        m, v = s["matches"][rank - 1], j["verdicts"][rank - 1]
        second = f" · 2차 {rc_v[(i, rank - 1)]}" if (i, rank - 1) in rc_v else ""
        md += [f"## {n_}. AI 판정: {v['v']}{second} ({rank}위) — {v['why']}", "",
               f"- **분실 신고** {r['atcId']}: {r.get('lstSbjt')} / {r.get('lstPrdtNm')} / {r.get('clrNm')} / "
               f"{r.get('lstLctNm')} {r.get('lstPlace')} / {r.get('lstYmd')} {r.get('lstHor', '')}시"
               + (f" / [사진]({r['lstFilePathImg']})" if r.get("lstFilePathImg") else ""),
               f"- **습득물** {m['atc_id']}: {m.get('name')} / {m.get('color')} / {m.get('found_at')} / "
               f"{m.get('found_place')} / {m.get('dep_place')} — 특이사항: {_desc(m) or '없음'}"
               + (f" / [사진]({m['image_url']})" if "no_img" not in (m.get("image_url") or "no_img") else ""),
               "- 내 판단: ", ""]
    (RAW / "review.md").write_text("\n".join(md), encoding="utf-8")
    print((OUT / "lost112.md").read_text(encoding="utf-8"))
    print(f"지켜볼 후보 {len(watch)}개(strong {sum(1 for w in watch if w['v'] == 'strong')}), "
          f"확인용 {len(pick)}건 → {RAW / 'review.md'}")


def watch() -> None:
    """지켜볼 후보가 그 뒤 종결(주인 인계 등으로 목록에서 내려감)됐는지 — 운영 found_closures를 읽기만.

    PROD_SSH(예: root@서버) 필요. strong 후보 종결률을 같은 검색의 none 후보(비슷한 습득물) 종결률과 견줌.
    종결 = 주인이 찾아갔을 수도, 보관 기간이 끝났을 수도 → 차이만 보고 '우리가 찾아 줬다'고 말하지 않음.
    """
    import os
    import re
    import subprocess

    host = os.environ.get("PROD_SSH")
    if not host:
        raise SystemExit("PROD_SSH 환경변수(운영 서버 ssh 주소)가 필요")
    w = json.loads((RAW / "watch.json").read_text(encoding="utf-8"))
    keys = sorted({x["key"] for x in w["items"] if re.fullmatch(r"[A-Za-z0-9_]+", x["key"])})
    made = w["made"][:10]
    sql = ("select item_id, kind, detected_at from found_closures where item_id in ("
           + ",".join(f"'{k}'" for k in keys) + f") and detected_at > '{made}';")  # 다음 밤 대조부터
    res = subprocess.run(["ssh", "-o", "ConnectTimeout=15", host,
                          "docker exec -i lostkorea-postgres psql -U lostkorea -d lostkorea -At -F '|'"],
                         input=sql, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if res.returncode:
        raise SystemExit(res.stderr[:500])
    closed = {ln.split("|")[0]: ln.split("|")[1:] for ln in res.stdout.splitlines() if "|" in ln}
    lines = [f"지켜보기 — 목록 만든 때 {w['made']}, 확인 {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    for v in ("strong", "possible", "none"):
        ks = {x["key"] for x in w["items"] if x["v"] == v}
        c = [k for k in ks if k in closed]
        kinds = collections.Counter(closed[k][0] for k in c)
        rate = f"{len(c) / len(ks):.1%}" if ks else "-"
        lines.append(f"- {v}: 후보 {len(ks)}개 중 종결 {len(c)}개 ({rate}) {dict(kinds)}")
    out = RAW / f"watch_{dt.date.today():%Y%m%d}.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ── 다국어 ───────────────────────────────────────────────────────────
LANGS = {"en": "English", "zh-CN": "Simplified Chinese", "ja": "Japanese"}
TR_PROMPT = """Rewrite this Korean lost-item report in {language}, the way a foreign visitor in Korea would type it
into a lost-and-found website. Keep every fact (date, hour, item, color, brand, bus/line numbers, place).
Write Korean place names the way a visitor would (romanized, or the usual name in {language}).
Do not add or drop information. Reply JSON only: {{"text": "..."}}

Korean report: {text}"""


def lang(n: int, workers: int) -> None:
    """같은 신고를 외국어로 넣어도 같은 습득물을 찾는지 — 번역은 AI(실제 외국인 글이 아닌 합성)라고 밝힘.

    고르기: 한국어에서 strong이 나온 신고 우선(비교할 정답 후보가 있는 신고) + 나머지 무작위로 n건.
    잼: 외국어 1위가 한국어 1위와 같은가, 한국어 strong 후보가 외국어 상위 8개·1위에 있는가.
    """
    from apps.api import gemini

    _count_tokens()
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl") if not s.get("error")}
    jud = {j["atcId"]: j for j in _load(RAW / "judge.jsonl") if not j.get("error")}
    strong_ids = [i for i in srch if i in jud and _first(jud[i]["verdicts"], {"strong"})]
    rest = [i for i in srch if i not in strong_ids and srch[i].get("matches")]
    rng = random.Random(3)
    pick = strong_ids[:n] + rng.sample(rest, max(0, min(len(rest), n - len(strong_ids))))
    out_p = RAW / "lang.jsonl"
    done = {(x["atcId"], x["lang"]): x for x in _load(out_p)} if out_p.exists() else {}
    jobs = [(i, lg) for i in pick for lg in LANGS if (i, lg) not in done or done[(i, lg)].get("error")]

    def one(job: tuple[str, str]) -> dict:
        i, lg = job
        from apps.agent import graph

        res = gemini.generate_json(TR_PROMPT.format(language=LANGS[lg], text=srch[i]["text"]))
        text = (res or {}).get("text", "") if isinstance(res, dict) else ""
        if not text:
            return {"atcId": i, "lang": lg, "error": "번역 실패"}
        try:
            st = graph.run(text, lost_date=rows[i].get("lstYmd"), lang=lg)
        except Exception as e:  # noqa: BLE001
            return {"atcId": i, "lang": lg, "text": text, "error": f"{type(e).__name__}: {e}"[:200]}
        return {"atcId": i, "lang": lg, "text": text,
                "is_lost_report": (st.get("extracted") or {}).get("is_lost_report", True),
                "keys": [f"{m['atc_id']}_{m['fd_sn']}" for m in (st.get("matches") or [])]}

    print(f"다국어 {len(jobs)}건({len(pick)}건 × {len(LANGS)}개 언어)")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, res in enumerate(ex.map(one, jobs), 1):
            done[(res["atcId"], res["lang"])] = res
            if k % 30 == 0 or k == len(jobs):
                _save(out_p, list(done.values()))
                print(f"  {k}/{len(jobs)}")

    lines = ["# 실제 분실 신고 — 외국어로 넣었을 때", "",
             f"- 신고 {len(pick)}건(한국어에서 strong이 나온 {min(len(strong_ids), n)}건 우선)을 AI가 {', '.join(LANGS.values())}로 바꿔 넣음",
             "- 번역은 AI 합성(실제 외국인 글 아님). 한국어 결과와 같은 습득물을 찾는지만 봄", "",
             "| 언어 | 신고 | 신고로 받음 | 1위가 한국어 1위와 같음 | 한국어 strong 후보가 8개 안 | 그 후보가 1위 |",
             "|---|---|---|---|---|---|"]
    for lg in LANGS:
        xs = [done[(i, lg)] for i in pick if (i, lg) in done and not done[(i, lg)].get("error")]
        ko_top = {i: (f"{srch[i]['matches'][0]['atc_id']}_{srch[i]['matches'][0]['fd_sn']}" if srch[i]["matches"] else "")
                  for i in pick}
        same = sum(1 for x in xs if x["keys"] and x["keys"][0] == ko_top[x["atcId"]])
        sx = [x for x in xs if x["atcId"] in strong_ids]
        hit = top1 = 0
        for x in sx:
            r = _first(jud[x["atcId"]]["verdicts"], {"strong"})
            m = srch[x["atcId"]]["matches"][r - 1]
            key = f"{m['atc_id']}_{m['fd_sn']}"
            hit += key in x["keys"]
            top1 += bool(x["keys"]) and x["keys"][0] == key
        acc = sum(1 for x in xs if x.get("is_lost_report", True))

        def p(a: int, b: int) -> str:
            return f"{a}/{b} ({a / b:.0%})" if b else "-"
        lines.append(f"| {LANGS[lg]} | {len(xs)} | {p(acc, len(xs))} | {p(same, len(xs))} | {p(hit, len(sx))} | {p(top1, len(sx))} |")
    # 보수적 하한(2차 confirmed — 양쪽에 적힌 구체적 특징이 맞는 후보)만으로 다시: 흔한 물건끼리 순위가 바뀌는 잡음을 뺌
    conf: dict[str, set[str]] = {}
    rc_p = RAW / "recheck.jsonl"
    for x in (_load(rc_p) if rc_p.exists() else []):
        if x.get("v") == "confirmed" and x["atcId"] in pick:
            m = srch[x["atcId"]]["matches"][x["idx"]]
            conf.setdefault(x["atcId"], set()).add(f"{m['atc_id']}_{m['fd_sn']}")
    if conf:
        lines += ["", f"보수적 하한 {len(conf)}건(2차 confirmed)만 — 그 습득물이 상위 8개 안 / 1위:", "",
                  "| 언어 | 8개 안 | 1위 |", "|---|---|---|"]
        for lg, name in (("ko", "한국어(원문)"), *LANGS.items()):
            hit = top = 0
            for i, keys in conf.items():
                got = ([f"{m['atc_id']}_{m['fd_sn']}" for m in srch[i]["matches"]] if lg == "ko"
                       else (done.get((i, lg)) or {}).get("keys") or [])
                hit += bool(keys & set(got))
                top += bool(got) and got[0] in keys
            lines.append(f"| {name} | {hit}/{len(conf)} | {top}/{len(conf)} |")
    (OUT / "lost112_lang.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"AI 호출 {TOKENS}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "sample", "search", "judge", "recheck", "report", "watch", "lang"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=112)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.cmd == "fetch":
        fetch()
    elif a.cmd == "sample":
        sample(a.n, a.seed)
    elif a.cmd == "search":
        search(a.workers)
    elif a.cmd == "judge":
        judge(a.workers)
    elif a.cmd == "recheck":
        recheck(a.workers)
    elif a.cmd == "watch":
        watch()
    elif a.cmd == "lang":
        lang(min(a.n, 100), a.workers)
    else:
        report()


if __name__ == "__main__":
    main()
