"""실제 분실 신고로 재는 실측 — 경찰청 분실물정보(LOST112)에 올라온 분실 신고를 우리 검색에 그대로 넣어 본다.

제한된 문항(직접 만든 질문)이 아니라 실제 주인이 쓴 신고로, "이미 보관 중인 습득물을 우리가 찾아 주는가"를 잰다.

  uv run python -m eval.run_lost112 fetch                 # 분실 신고 목록 받기(오늘 날짜 스냅숏 — 매일 돌리면 사라진 신고 추적)
  uv run python -m eval.run_lost112 sample --n 300        # 분류 비례(최소 5) 무작위 표본 + 상세 받기
  uv run python -m eval.run_lost112 search                # 실제 서비스 흐름(추출→지역→검색→판정 8개) — OPENSEARCH_URL 필요
  uv run python -m eval.run_lost112 judge                 # 상위 8개를 다른 모델로 엄격 판정(강함/가능/없음)
  uv run python -m eval.run_lost112 report                # 집계(eval/results/lost112.md) + 지켜볼 목록 + 운영자 확인 30건
  PROD_SSH=root@서버 uv run python -m eval.run_lost112 watch   # 그 뒤 후보가 종결됐는지(운영 종결 기록 읽기만)
  PROD_SSH=root@서버 uv run python -m eval.run_lost112 lostwatch  # 양쪽 종결 일치: 표본 신고 종결(접수해지) × 후보 습득물 종결
  PROD_SSH=root@서버 uv run python -m eval.run_lost112 closedrev  # 거꾸로: 목록에서 사라진(종결) 신고 → 같은 물건 습득물도 종결?
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


def _with_detail(r: dict) -> dict:
    """목록 한 줄 + 상세(분실 지역·접수 경찰서·상태 등). 가린 특이사항·'사진 없음' 그림은 비움."""
    d = (_items(_call("getLostGoodsDetailInfo", ATC_ID=r["atcId"])) or [{}])[0]
    m = {**r, **{k: v for k, v in d.items() if v}}
    if HIDDEN in m.get("uniq", ""):
        m["uniq"] = ""
    if "no_img" in m.get("lstFilePathImg", ""):  # 사진 없음 자리표시 그림
        m["lstFilePathImg"] = ""
    return m


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
        out = list(ex.map(_with_detail, picked))
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
    if ex.get("degraded"):  # AI 추출 실패(429 등) — 조용히 깨진 결과를 집계에 섞지 않게(11/17)
        return {"atcId": r["atcId"], "text": text, "error": "추출 실패(degraded)"}
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


def _judge_cands(r: dict, ms: list[dict], think: str = "") -> tuple[list[dict] | None, str]:
    """신고 하나 × 후보들 → ([{v, why}] 후보 순서대로, 오류). think='low'면 생각을 줄여 비용을 낮춤(3.8-flash는 minimal 거부)."""
    from apps.api import gemini

    if not ms:
        return [], ""
    gen: dict = {"responseMimeType": "application/json", "temperature": 0}
    if think:
        gen["thinkingConfig"] = {"thinkingLevel": think}
    body = {"contents": [{"parts": [{"text": JUDGE_PROMPT.format(lost=_lost_block(r), cands=_cand_block(ms))}]}],
            "generationConfig": gen}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_MODEL}:generateContent"
    err = ""
    for attempt in range(3):
        try:
            res = gemini._post(url, body, timeout=90)
            data = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
            vs = {int(str(v["id"]).lstrip("#")): v for v in data.get("verdicts", [])
                  if str(v.get("id", "")).lstrip("#").isdigit()}
            return [{"v": (vs.get(i) or {}).get("v", "none"), "why": (vs.get(i) or {}).get("why", "")}
                    for i in range(len(ms))], ""
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
            time.sleep(3 * (attempt + 1))
    return None, err


def _judge_one(pair: tuple[dict, dict]) -> dict:
    r, s = pair
    vs, err = _judge_cands(r, s.get("matches") or [])
    return {"atcId": r["atcId"], "verdicts": vs} if vs is not None else {"atcId": r["atcId"], "error": err}


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


def _prod_psql(sql: str) -> str:
    """운영 PostgreSQL에 읽기 질의 하나(ssh) — PROD_SSH 필요. 쓰기는 하지 않음(원격 쓰기는 운영자가)."""
    import os
    import subprocess

    host = os.environ.get("PROD_SSH")
    if not host:
        raise SystemExit("PROD_SSH 환경변수(운영 서버 ssh 주소)가 필요")
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
                          "docker exec -i lostkorea-postgres psql -U lostkorea -d lostkorea -q -At -F '|'"],
                         input=sql, capture_output=True, text=True, encoding="utf-8", timeout=180)
    if res.returncode:
        raise SystemExit(res.stderr[:500])
    return res.stdout


def _found_closed(keys: list[str], since: str) -> dict[str, list[str]]:
    """운영 found_closures에서 이 습득물들의 종결 기록(since 다음 날부터) — {키: [종류, 감지일]}. PROD_SSH 필요, 읽기만."""
    import re

    keys = sorted({k for k in keys if re.fullmatch(r"[A-Za-z0-9_]+", k)})
    if not keys:
        return {}
    out = _prod_psql("select item_id, kind, detected_at from found_closures where kind = 'closed' and item_id in ("
                     + ",".join(f"'{k}'" for k in keys) + f") and detected_at > '{since}';")  # 다음 밤 대조부터, 사진 내려감 제외
    return {ln.split("|")[0]: ln.split("|")[1:] for ln in out.splitlines() if "|" in ln}


def watch() -> None:
    """지켜볼 후보가 그 뒤 종결(주인 인계 등으로 목록에서 내려감)됐는지 — 운영 found_closures를 읽기만.

    PROD_SSH(예: root@서버) 필요. strong 후보 종결률을 같은 검색의 none 후보(비슷한 습득물) 종결률과 견줌.
    종결 = 주인이 찾아갔을 수도, 보관 기간이 끝났을 수도 → 차이만 보고 '우리가 찾아 줬다'고 말하지 않음.
    """
    w = json.loads((RAW / "watch.json").read_text(encoding="utf-8"))
    closed = _found_closed([x["key"] for x in w["items"]], w["made"][:10])
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


def lostwatch() -> None:
    """양쪽 종결 일치(회수 추정) — 표본 분실 신고가 목록에서 빠져 '종결(접수해지)'이 됐는지 + 그 신고의 후보 습득물이 종결됐는지.

    신고 쪽 종결만으로는 '찾음'인지 '신고 취소'인지 모름(사유가 하나) → 그 신고의 strong 후보 습득물도 종결됐는지 겹쳐 보고,
    같은 신고의 none 후보(비슷한 습득물) 종결과 견줘 우연히 겹친 비율을 가늠. data.go.kr 목록 23쪽 + 빠진 신고 상세만(AI 0회).
    수집 시간(0~5시)엔 data.go.kr 429가 잦아 피할 것. 말할 땐 '회수 확정'이 아니라 '양쪽 종결 일치(회수 추정)'.
    """
    rows = {r["atcId"]: r for r in _load(RAW / "sample.jsonl")}
    listed = {r["atcId"] for r in _load(fetch())}
    sp = RAW / "lost_status.json"
    status = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    today = f"{dt.date.today():%Y-%m-%d}"
    for i in rows:
        if i in listed or status.get(i, {}).get("closed"):
            continue
        d = (_items(_call("getLostGoodsDetailInfo", ATC_ID=i)) or [{}])[0]
        status[i] = {"closed": "종결" in d.get("lstSteNm", ""), "ste": d.get("lstSteNm", ""), "seen": today}
    sp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

    w = json.loads((RAW / "watch.json").read_text(encoding="utf-8"))
    found = _found_closed([x["key"] for x in w["items"]], w["made"][:10])
    by_lost: dict[str, list[dict]] = collections.defaultdict(list)
    for x in w["items"]:
        by_lost[x["lost"]].append(x)
    lost_closed = {i for i, s in status.items() if s.get("closed")}

    def rate(xs: list[dict]) -> str:
        c = sum(1 for x in xs if x["key"] in found)
        return f"{c}/{len(xs)} ({c / len(xs):.1%})" if xs else "-"

    strong_reports = [i for i in rows if any(x["v"] == "strong" for x in by_lost.get(i, []))]
    both = [i for i in strong_reports
            if i in lost_closed and any(x["v"] == "strong" and x["key"] in found for x in by_lost[i])]
    lines = [f"# 양쪽 종결 일치 — 확인 {dt.datetime.now():%Y-%m-%d %H:%M} (후보 목록 {w['made']})", "",
             f"- 표본 신고 {len(rows)}건 중 신고 종결(접수해지): {len(lost_closed)}건",
             f"- strong 후보가 있는 신고 {len(strong_reports)}건 중 신고 종결 "
             f"{sum(1 for i in strong_reports if i in lost_closed)}건, **그중 strong 후보 습득물도 종결 {len(both)}건**", "",
             "후보 습득물 종결률(우연히 겹친 비율을 가늠하는 대조):", "",
             "| 후보 | 신고가 종결된 신고의 후보 | 신고가 열린 신고의 후보 |", "|---|---|---|"]
    for v in ("strong", "possible", "none"):
        xs_c = [x for i in lost_closed for x in by_lost.get(i, []) if x["v"] == v]
        xs_o = [x for i in rows if i not in lost_closed for x in by_lost.get(i, []) if x["v"] == v]
        lines.append(f"| {v} | {rate(xs_c)} | {rate(xs_o)} |")
    lines += ["", "양쪽 종결 일치(strong) 건별:", ""]
    for i in both:
        ks = [x for x in by_lost[i] if x["v"] == "strong" and x["key"] in found]
        lines.append(f"- {i} {rows[i].get('lstPrdtNm', '')[:20]} — 신고 종결 확인 {status[i]['seen']} / 습득물 "
                     + ", ".join(f"{x['key']}({x['rank']}위, 종결 감지 {found[x['key']][1]})" for x in ks))
    out = RAW / f"lostwatch_{dt.date.today():%Y%m%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


# ── 거꾸로: 이미 종결된 분실 신고에서 출발 ───────────────────────────────
REV_P = RAW / "closedrev.json"
PRICE = (0.75e-6, 3.75e-6)  # 3.6·3.8 Flash 1M 토큰당 입력 $0.75 · 출력(생각 포함) $3.75 — 2026-11 기준


def _cost() -> float:
    return TOKENS["in"] * PRICE[0] + TOKENS["out"] * PRICE[1]


def _closures(since: str) -> list[dict]:
    """운영 found_closures 중 종결(습득일 since 이후) = 이미 우리 색인에서 지워진 습득물. 이름·특이사항은 안 남기므로
    판정 전에 경찰 상세로 다시 받음(종결된 습득물도 상세는 '종결' 상태로 그대로 나옴 — 11/18 확인). 읽기만."""
    import csv
    import io

    dt.date.fromisoformat(since)  # 질의에 넣기 전 날짜 꼴 확인
    out = _prod_psql("\\copy (select item_id, source, category_top, category_sub, color, region, dep_place, found_place, "
                     f"found_at, detected_at from found_closures where kind = 'closed' and found_at >= '{since}') "
                     "to stdout with csv header\n")
    return list(csv.DictReader(io.StringIO(out)))


def _gap(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b[:10]) - dt.date.fromisoformat(a[:10])).days


def _pool(r: dict, region_set: list[str], closures: list[dict], skip: set[str], cap: int) -> list[dict]:
    """이미 종결된 습득물 중 이 신고와 이어질 만한 것 — 같은 대분류, 분실일 하루 전~30일 뒤 습득, 지역이 이어짐
    (우리 검색이 낸 경유 지역 또는 신고 시도). 거친 거름이라 순서는 경유 지역·소분류·색·날짜 가까움으로만 매김(검색 순위 아님)."""
    from apps.api.region import _CANON_SIDO, region_matches

    top, _, sub = (r.get("prdtClNm") or "").partition(">")
    top, sub = top.strip(), sub.strip()
    sido = (r.get("lstLctNm") or "").split(" ")[0]
    # 분실 신고는 시도를 옛 이름·통합 이름으로도 씀('강원도', '전남광주통합특별시') — 습득물 쪽 이름(강원특별자치도 등)으로
    sidos = {"광주광역시", "전라남도"} if sido == "전남광주통합특별시" else {_CANON_SIDO.get(sido, sido)} - {""}
    color = (r.get("clrNm") or "").split("(")[0]
    lost = r.get("lstYmd", "")
    if not (top and lost):
        return []
    out = []
    for c in closures:
        if c["item_id"] in skip or c["category_top"] != top or not c.get("found_at"):
            continue
        gap = _gap(lost, c["found_at"])
        if not -1 <= gap <= 30:
            continue
        on_route = bool(region_set) and region_matches(c.get("region", ""), region_set)
        if not on_route and ((c.get("region") or "").split() or [""])[0] not in sidos:  # 지역 빈 기록 있음
            continue
        out.append(((not on_route, c["category_sub"] != sub, not (color and c.get("color", "").startswith(color)), abs(gap)), c))
    out.sort(key=lambda x: x[0])
    return [c for _, c in out[:cap]]


def _closed_doc(c: dict) -> dict:
    """종결 기록 + 경찰 상세(이름·특이사항·습득장소·보관 경찰서) → 판정 후보 꼴(_cand_block과 같은 칸)."""
    from apps.collector import client

    atc, _, sn = c["item_id"].rpartition("_")
    try:
        d = client.get_detail(c.get("source") or "police", atc, sn)
    except Exception:  # noqa: BLE001 — 상세를 못 받아도 분류·색·장소·날짜로는 판정
        d = {}
    return {"atc_id": atc, "fd_sn": sn, "name": d.get("fdPrdtNm", ""),
            "category": f"{c['category_top']} > {c['category_sub']}", "color": c.get("color", ""),
            "description": d.get("uniq", ""), "dep_place": c.get("dep_place", ""),
            "found_place": d.get("fdPlace") or c.get("found_place", ""), "region": c.get("region", ""),
            "found_at": c.get("found_at", ""), "org_name": d.get("orgNm", ""),
            "closed_at": c.get("detected_at", ""), "ste": d.get("csteSteNm", "")}


def _save_rev(st: dict) -> None:
    REV_P.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def closedrev(cap: int, budget: float, n_control: int, stage: str = "all") -> None:
    """거꾸로 보기 — 전체 분실 신고 목록에서 사라진(종결) 신고에서 출발해, 같은 물건으로 보이는 습득물도 함께 종결됐는지 본다.

    표본 300건을 기다리면 하루 0.2건꼴만 종결돼(11/18: 전체 22,024건 중 하루 24건) 결선 전에 숫자가 안 나와서 바꿈.
    1) 어제 목록에 있고 오늘 목록에 없는 신고 → 상세가 '종결'인 것만(종결 사유는 회수·취소 구분 없음).
    2) 우리 검색(지금 색인, 서비스와 같은 흐름) 상위 8개를 판정 → 같은 물건 후보가 다음 밤 대조에서 종결되는지(라이브).
    3) 이미 종결돼 색인에서 지워진 습득물(운영 found_closures)도 같은 분류·지역·기간으로 걸러 판정(종결 풀) —
       습득물을 먼저 찾아가고 신고는 나중에 닫히면 2)에선 못 봄. 이 풀은 우리 검색 순위가 아니라 거친 거름.
    4) 대조군: 아직 열린 신고(표본 300 중)에 같은 거름·같은 판정 → 종결 풀에 같은 물건이 '우연히' 있는 비율.
    말할 땐 '회수 확정'이 아니라 '양쪽 종결 일치(회수 추정)'. AI 비용은 budget 달러에서 멈춤.
    """
    _count_tokens()
    if stage in ("live", "all"):
        try:  # 색인에 못 붙으면 AI(추출·지역)만 쓰고 검색에서 전부 실패함(11/18 첫 실행) — 먼저 확인
            urllib.request.urlopen(settings.opensearch_url, timeout=10).close()
        except OSError as e:
            raise SystemExit(f"색인 {settings.opensearch_url}에 못 붙음({e}) — OPENSEARCH_URL·터널 확인") from e
    st = json.loads(REV_P.read_text(encoding="utf-8")) if REV_P.exists() else {"reports": {}, "control": {}}
    cur_p = fetch()
    prevs = [p for p in sorted(RAW.glob("list_*.jsonl")) if p.name < cur_p.name]
    if not prevs:
        raise SystemExit("비교할 이전 목록이 없음 — 내일 다시")
    prev = {r["atcId"]: r for r in _load(prevs[-1])}
    cur_ids = {r["atcId"] for r in _load(cur_p)}
    today = f"{dt.date.today():%Y-%m-%d}"
    gone = [prev[i] for i in prev if i not in cur_ids and i not in st["reports"]]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for m in ex.map(_with_detail, gone):
            st["reports"][m["atcId"]] = {"row": m, "ste": m.get("lstSteNm", ""), "closed": "종결" in m.get("lstSteNm", ""),
                                         "gone": f"{prevs[-1].stem[5:]}~{cur_p.stem[5:]}", "seen": today}
    _save_rev(st)
    todo = [i for i, x in st["reports"].items() if x["closed"] and "verdicts" not in x]
    print(f"사라진 신고 {len(gone)}건(새로) — 그중 종결 {sum(1 for r in gone if st['reports'][r['atcId']]['closed'])}건, "
          f"검색·판정할 것 {len(todo)}건")
    # 1단계(라이브) — 밤 대조(0시) 전에 돌려야 오늘 종결된 습득물이 아직 색인에 있음
    done_now = 0
    for i in todo if stage in ("live", "all") else []:
        if _cost() > budget:
            print(f"  예산 {budget}달러 도달 — 남은 {len(todo) - done_now}건은 다음에")
            break
        x, r = st["reports"][i], st["reports"][i]["row"]
        s = _search_one(r)
        lv, err = _judge_cands(r, s.get("matches") or [], think="low") if not s.get("error") else (None, s["error"])
        if lv is None:
            x["error"] = err
            print(f"  ! {i} 실패 — {err}")
            continue
        x.pop("error", None)
        x.update(searched=today, text=s["text"], region_set=s["region_set"], live=s["matches"], verdicts=lv)
        done_now += 1
        _save_rev(st)
        print(f"  {done_now}/{len(todo)} {i} — 색인 후보 strong {sum(v['v'] == 'strong' for v in lv)}/{len(lv)}"
              f" · 누적 ${_cost():.3f}")
    if stage == "live":
        print(f"AI 호출 {TOKENS} ≈ ${_cost():.3f}")
        _rev_report(st)
        return

    # 2단계(종결 풀) — 이미 지워진 습득물. 기록·상세가 남아 급하지 않음
    sample_rows = _load(RAW / "sample.jsonl")
    pool_todo = [i for i, x in st["reports"].items() if "verdicts" in x and "pool_verdicts" not in x]
    lost_days = [st["reports"][i]["row"].get("lstYmd", "") for i in pool_todo] + [r.get("lstYmd", "") for r in sample_rows]
    since = str(dt.date.fromisoformat(min(d for d in lost_days if d)) - dt.timedelta(days=1))
    closures = _closures(since)
    print(f"종결 습득물 기록 {len(closures)}건(습득일 {since} 이후) — 종결 풀 판정 {len(pool_todo)}건")
    pooled = 0
    for i in pool_todo:
        if _cost() > budget:
            print(f"  예산 {budget}달러 도달 — 종결 풀은 여기까지")
            break
        x = st["reports"][i]
        pool = [_closed_doc(c) for c in _pool(x["row"], x["region_set"], closures,
                                              {f"{m['atc_id']}_{m['fd_sn']}" for m in x["live"]}, cap)]
        pv, err = _judge_cands(x["row"], pool, think="low")
        if pv is None:
            print(f"  ! {i} 종결 풀 판정 실패 — {err}")
            continue
        x.update(pool=pool, pool_verdicts=pv, pooled=today)
        pooled += 1
        _save_rev(st)

    # 대조군: 아직 열린 표본 신고 — 이번에 종결 풀을 본 신고와 같은 수만큼(무작위, 날짜로 고정)
    srch = {s["atcId"]: s for s in _load(RAW / "search.jsonl") if not s.get("error")}
    opened = [r for r in sample_rows if r["atcId"] in cur_ids and r["atcId"] not in st["control"] and r["atcId"] in srch]
    k = n_control if n_control >= 0 else pooled
    for r in random.Random(today).sample(opened, min(k, len(opened))):
        if _cost() > budget:
            print(f"  예산 {budget}달러 도달 — 대조군은 여기까지")
            break
        pool = [_closed_doc(c) for c in _pool(r, srch[r["atcId"]].get("region_set") or [], closures, set(), cap)]
        pv, err = _judge_cands(r, pool, think="low")
        if pv is None:
            print(f"  ! 대조 {r['atcId']} 판정 실패 — {err}")
            continue
        st["control"][r["atcId"]] = {"date": today, "row": r, "pool": pool, "pool_verdicts": pv}
        _save_rev(st)
    print(f"AI 호출 {TOKENS} ≈ ${_cost():.3f}")
    _rev_report(st)


def _rev_report(st: dict) -> None:
    """누적 집계 → data/lost112/closedrev_YYYYMMDD.md (신고·습득물 내용이 들어가므로 저장소 밖)."""
    rep = st["reports"]
    closed = [x for x in rep.values() if x["closed"]]
    done = [x for x in closed if "verdicts" in x]
    keys = [f"{m['atc_id']}_{m['fd_sn']}" for x in done for m in x["live"]]
    later = _found_closed(keys, min(x["searched"] for x in done)) if done else {}

    def after(x: dict, m: dict) -> str:  # 검색한 날 뒤 밤 대조에서 종결 감지된 날
        k = later.get(f"{m['atc_id']}_{m['fd_sn']}")
        return k[1] if k and k[1] > x["searched"] else ""

    def has(x: dict, field: str, v: str) -> bool:
        return any(y["v"] == v for y in x.get(field) or [])

    def pct(a: int, b: int) -> str:
        return f"{a}/{b} ({a / b:.0%})" if b else "-"

    ctl = list(st["control"].values())
    live_strong = [x for x in done if has(x, "verdicts", "strong")]
    live_both = [x for x in live_strong
                 if any(v["v"] == "strong" and after(x, m) for m, v in zip(x["live"], x["verdicts"], strict=True))]
    lines = [f"# 거꾸로 보기 — 종결된 분실 신고에서 출발 (확인 {dt.datetime.now():%Y-%m-%d %H:%M})", "",
             f"- 목록에서 사라진 신고 {len(rep)}건 중 상세 '종결' {len(closed)}건, 검색·판정 {len(done)}건 "
             f"(다른 상태: {dict(collections.Counter(x['ste'] for x in rep.values() if not x['closed']))})",
             "- 종결 사유는 회수·취소 구분이 없음 → '양쪽 종결 일치(회수 추정)'로만 말함", "",
             "## 1. 우리 검색(지금 색인)이 짚은 습득물", "",
             f"- 같은 물건(strong) 후보가 있는 신고: {pct(len(live_strong), len(done))}",
             f"- 그 strong 후보가 검색 뒤 밤 대조에서 종결: {pct(len(live_both), len(live_strong))} "
             f"(검색한 날 다음 밤부터 반영 — 오늘 검색한 건 내일 확인)", ""]
    for v in ("strong", "possible", "none"):
        xs = [(x, m) for x in done for m, y in zip(x["live"], x["verdicts"], strict=True) if y["v"] == v]
        lines.append(f"  - {v} 후보 종결률(대조): {pct(sum(1 for x, m in xs if after(x, m)), len(xs))}")
    ctl_strong = [x for x in ctl if has(x, "pool_verdicts", "strong")]
    lines += ["", "## 2. 이미 종결돼 색인에서 지워진 습득물(같은 분류·지역·기간 거름) 중 같은 물건", "",
              "| 신고 | 신고 수 | strong 있음 | possible 이상 있음 | 풀 평균 크기 |", "|---|---|---|---|---|"]
    for name, xs in (("종결된 신고", [x for x in done if "pool_verdicts" in x]), ("열린 신고(대조군)", ctl)):
        avg = sum(len(x.get("pool") or []) for x in xs) / len(xs) if xs else 0
        lines.append(f"| {name} | {len(xs)} | {pct(sum(1 for x in xs if has(x, 'pool_verdicts', 'strong')), len(xs))} | "
                     f"{pct(sum(1 for x in xs if has(x, 'pool_verdicts', 'strong') or has(x, 'pool_verdicts', 'possible')), len(xs))}"
                     f" | {avg:.1f} |")
    lines += ["", f"(대조군 strong {len(ctl_strong)}건 — 열린 신고인데 종결 풀에 같은 물건이 있다고 본 경우 = 판정의 우연·과신 정도)",
              "", "## 건별(strong)", ""]
    for x in done:
        r = x["row"]
        pairs = [("색인", m, after(x, m) or "열림") for m, v in zip(x["live"], x["verdicts"], strict=True) if v["v"] == "strong"]
        pairs += [("종결 풀", m, m.get("closed_at", ""))
                  for m, v in zip(x.get("pool") or [], x.get("pool_verdicts") or [], strict=True) if v["v"] == "strong"]
        if not pairs:
            continue
        lines.append(f"- **{r['atcId']}** {r.get('lstSbjt', '')[:40]} / {r.get('lstPrdtNm')} / {r.get('lstLctNm', '')} "
                     f"{r.get('lstPlace', '')} / 분실 {r.get('lstYmd')} / 접수 {r.get('orgNm', '')} — 신고 사라짐 {x['gone']}")
        for src, m, when in pairs:
            same = " · **같은 경찰서**" if r.get("orgNm") and r.get("orgNm") == m.get("org_name") else ""
            lines.append(f"  - [{src}] {m['atc_id']}_{m['fd_sn']} {m.get('name', '')[:30]} / {m.get('color')} / "
                         f"습득 {m.get('found_at')} / {m.get('dep_place')}({m.get('org_name', '')}) / 종결 {when}{same} — "
                         f"{_desc(m)[:80] or '특이사항 없음'}")
    out = RAW / f"closedrev_{dt.date.today():%Y%m%d}.md"
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
    ap.add_argument("cmd", choices=["fetch", "sample", "search", "judge", "recheck", "report", "watch", "lostwatch",
                                    "closedrev", "lang"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=112)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cap", type=int, default=12, help="closedrev: 신고마다 종결 풀 후보 상한")
    ap.add_argument("--budget", type=float, default=0.2, help="closedrev: 이번 실행 AI 비용 상한(달러)")
    ap.add_argument("--control", type=int, default=-1, help="closedrev: 대조군 수(-1 = 이번에 판정한 종결 신고 수만큼)")
    ap.add_argument("--stage", choices=["live", "pool", "all"], default="all",
                    help="closedrev: live = 우리 검색만(밤 대조 전에), pool = 이미 지워진 습득물 + 대조군")
    a = ap.parse_args()
    if a.cmd == "closedrev":
        closedrev(a.cap, a.budget, a.control, a.stage)
        return
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
    elif a.cmd == "lostwatch":
        lostwatch()
    elif a.cmd == "lang":
        lang(min(a.n, 100), a.workers)
    else:
        report()


if __name__ == "__main__":
    main()
