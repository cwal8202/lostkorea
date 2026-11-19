"""습득물 영어 번역(FIND-124) — 수집 때 한 번, 사람이 쓴 글(물품명·특이사항·경찰 아닌 보관 기관)만 AI로 옮겨 색인에 저장.

왜 이렇게(11/19 실무 조사·운영 데이터 실측):
- 정해진 낱말(분류·색·습득 장소·경찰 기관)은 표·규칙(apps/api/vocab_en.py) — AI 아님.
- 설명 문장의 99.5%는 '○○(색)을 습득하여 보관하고 있습니다' 상투 문장이라 화면이 이미 걷어냄 → 옮길 것은 물품명(평균 7.7자)과
  일부(3.6%)에만 있는 특이사항, 역·공항·쇼핑몰 같은 기관 이름.
- 같은 글은 번역 기억(tr_cache)으로 한 번만. 볼 때 10개씩 부르면 지시문이 매번 붙어 비싸고 화면이 기다림 → 수집 때 묶어서.
- 브랜드·모델명 보호: 원문의 영문·숫자 낱말이 번역에 그대로 없거나 한글이 남으면 버림(화면은 원문) — 범용 AI가 상품명의
  브랜드·모델 번호를 바꾸는 문제(월마트 EMNLP 2025).
색인 칸 name_mt·note_mt·org_mt·dep_mt(영어, 못 옮기면 ''). 다른 12개 언어는 화면 위 구글 번역이 이 영어(또는 한국어)를 옮김.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from apps import store
from apps.api import gemini
from apps.api.config import settings
from apps.api.desc import clean_desc
from apps.api.vocab_en import police_en

MT_FIELDS = ("name_mt", "note_mt", "org_mt", "dep_mt")
_SRC_FIELDS = ["name", "description", "org_name", "dep_place", "found_at"]
_PREFIX = {"name": "n", "note": "t", "place": "p"}
BATCH = 60          # 한 번에 옮기는 글 수(물품명 평균 7.7자 — 지시문 비용을 나눠 냄)
WORKERS = 4
_LATIN = re.compile(r"[A-Za-z0-9]+")
_HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")

_PROMPT = """You translate records from Korea's police lost-and-found database into short, natural English for foreign visitors.
The key prefix tells the kind: "n" = item name, "t" = police note about the item, "p" = place/organization keeping the item.
Rules:
- Keep every Latin letter, digit, brand and model name exactly as written ("DAKS 지갑" -> "DAKS wallet",
  "갤럭시s1 휴대폰" -> "Galaxy s1 phone", "IQOS 전자담배" -> "IQOS e-cigarette").
- Korean brands, banks and cards: their usual English names (신한카드 -> Shinhan Card, KB국민카드 -> KB Kookmin Card,
  카카오뱅크 -> KakaoBank, 삼성 -> Samsung, 티머니 -> T-money, 기후동행카드 -> Climate Card).
- Personal names: romanized, masking kept (김O숙 -> Kim O-suk).
- Places: official English names (서울역 -> Seoul Station, 한국철도공사 -> Korail, 신분당선 -> Shinbundang Line,
  2호선 -> Line 2, 유실물센터 -> Lost & Found Center, 인천국제공항 -> Incheon International Airport); romanize other Korean names.
- Notes: translate the whole note faithfully and briefly; never add or drop details.
- No Korean characters in the output. Output one JSON object with the same keys and English strings.

{payload}"""

STATS = {"calls": 0, "in": 0, "out": 0, "failed_calls": 0}


def valid(ko: str, en: str) -> bool:
    """번역을 믿을 만한가 — 한글이 안 남고, 원문의 영문·숫자 낱말(브랜드·모델 번호)이 그대로 있음."""
    if not en or not en.strip() or _HANGUL.search(en):
        return False
    low = en.lower()
    return all(tok.lower() in low for tok in _LATIN.findall(ko))


def _call(prompt: str):
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gemini._gen_config()}
    res = gemini._post(gemini._gen_url(), body, timeout=120)
    u = res.get("usageMetadata") or {}
    STATS["calls"] += 1
    STATS["in"] += u.get("promptTokenCount", 0)
    STATS["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
    return json.loads(res["candidates"][0]["content"]["parts"][0]["text"])


def translate(items: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """[(종류, 한국어)] → {(종류, 한국어): 영어 또는 ''(믿을 수 없음)}. 호출 실패면 {}(다음 밤에 다시)."""
    keys = {f"{_PREFIX[k]}{i}": (k, ko) for i, (k, ko) in enumerate(items)}
    try:
        res = _call(_PROMPT.format(payload=json.dumps({key: ko for key, (_, ko) in keys.items()},
                                                      ensure_ascii=False, indent=0)))
    except Exception:  # noqa: BLE001 — 실패한 묶음은 기억에 안 넣음 → 다음 밤에 다시
        STATS["failed_calls"] += 1
        return {}
    res = res if isinstance(res, dict) else {}
    out = {}
    for key, (k, ko) in keys.items():
        en = res.get(key)
        en = re.sub(r"\s+", " ", en).strip()[:300] if isinstance(en, str) else ""
        out[(k, ko)] = en if valid(ko, en) else ""
    return out


def _strings(s: dict) -> list[tuple[str, str]]:
    """문서 하나에서 AI로 옮길 글 — 물품명, 특이사항(상투 문장 걷어낸 것), 경찰 아닌 기관 이름."""
    out = [("name", (s.get("name") or "").strip())]
    note = clean_desc(s.get("description"))
    if note:
        out.append(("note", note))
    for f in ("org_name", "dep_place"):
        v = (s.get(f) or "").strip()
        if v and not police_en(v):
            out.append(("place", v))
    return [(k, v) for k, v in out if v]


def _fields(s: dict, known: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """색인에 넣을 영어 칸 — 아직 못 옮긴 글이 있으면 None(다음 밤에)."""
    for k, v in _strings(s):
        if v not in known[k]:
            return None
    name, note = (s.get("name") or "").strip(), clean_desc(s.get("description"))
    org, dep = (s.get("org_name") or "").strip(), (s.get("dep_place") or "").strip()
    return {"name_mt": known["name"].get(name, ""), "note_mt": known["note"].get(note, "") if note else "",
            "org_mt": police_en(org) or known["place"].get(org, ""),
            "dep_mt": police_en(dep) or known["place"].get(dep, "")}


def _ensure_mapping() -> None:
    from apps.collector.run import _os
    _os(f"/{settings.opensearch_index}/_mapping",
        {"properties": {f: {"type": "keyword", "ignore_above": 512} for f in MT_FIELDS}}, method="PUT")


def _bulk_update(updates: dict[str, dict], chunk: int = 1000) -> int:
    done = 0
    items = list(updates.items())
    for i in range(0, len(items), chunk):
        lines = []
        for _id, doc in items[i : i + chunk]:
            lines.append(json.dumps({"update": {"_index": settings.opensearch_index, "_id": _id}}))
            lines.append(json.dumps({"doc": doc}, ensure_ascii=False))
        req = urllib.request.Request(f"{settings.opensearch_url}/_bulk", ("\n".join(lines) + "\n").encode(),
                                     {"Content-Type": "application/x-ndjson"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
        done += sum(1 for it in res.get("items", []) if 200 <= (it.get("update") or {}).get("status", 500) < 300)
    return done


def sync(max_docs: int, budget_s: float, max_calls: int = 300, dry: bool = False) -> dict:
    """아직 영어 칸이 없는 습득물을 습득일 최신순으로 최대 max_docs건 — 기억에 없는 글만 AI로, 색인에 저장.

    상한 두 개: budget_s초, AI 호출 max_calls회(호출 1회 = 글 60개 ≈ $0.004 — 11/20 시험 199개 $0.0138).
    한 바퀴(묶음 4개)마다 번역 기억에 저장 → 중간에 끊겨도 다음 실행이 이어서(이미 옮긴 글은 기억에서).
    dry=True: AI 호출·결과까지만(기억·색인에 안 씀) — 비용·품질 시험용.
    """
    from apps.collector.run import _scan_docs

    docs = _scan_docs({"bool": {"must_not": [{"exists": {"field": "name_mt"}}]}}, _SRC_FIELDS)
    order = sorted(docs, key=lambda i: docs[i].get("found_at") or "", reverse=True)[:max_docs]
    wanted = list(dict.fromkeys(kv for i in order for kv in _strings(docs[i])))   # 최신 문서의 글부터
    known = {k: store.tr_get(k, [v for kk, v in wanted if kk == k]) for k in _PREFIX}
    todo = [kv for kv in wanted if kv[1] not in known[kv[0]]]
    st = {"docs": len(order), "missing_docs": len(docs), "strings": len(wanted), "cached": len(wanted) - len(todo),
          "to_translate": len(todo), "translated": 0, "rejected": 0, "updated": 0, "samples": []}
    batches = [todo[b : b + BATCH] for b in range(0, len(todo), BATCH)][:max_calls]
    start = time.monotonic()
    with ThreadPoolExecutor(WORKERS) as ex:      # 묶음 4개씩 동시에(분당 수십 회 — Tier 1 하루 1만 회에 한참 못 미침)
        for r in range(0, len(batches), WORKERS):
            if time.monotonic() - start > budget_s:
                break
            for got in ex.map(translate, batches[r : r + WORKERS]):
                for (k, ko), en in got.items():
                    known[k][ko] = en
                    st["translated"] += 1
                    st["rejected"] += 0 if en else 1
                    if dry and len(st["samples"]) < 60:
                        st["samples"].append((k, ko, en))
                if not dry:
                    for k in _PREFIX:
                        store.tr_put(k, {ko: en for (kk, ko), en in got.items() if kk == k})
    updates = {i: f for i in order if (f := _fields(docs[i], known)) is not None}
    if dry:
        st["would_update"] = len(updates)
    elif updates:
        _ensure_mapping()
        st["updated"] = _bulk_update(updates)
    st.update({f"ai_{k}": v for k, v in STATS.items()})
    return st
