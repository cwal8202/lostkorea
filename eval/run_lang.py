"""신고 결과 번역 측정 — 6개 언어로 같은 분실 신고를 넣고, 판정 결과의 물품명·설명·이유가 신고자 언어로 나오는지 잰다.

실제 Gemini 호출(비용 수 센트). 후보 = 운영 실제 습득물 12건(eval/data/lang_candidates.json — 지갑류, 개인정보 있는 건 제외).
검사 = apps/api/langcheck.lang_ok(문자 체계 기준).

  uv run python -m eval.run_lang --tag before --repeat 2     # 고치기 전 코드로
  uv run python -m eval.run_lang --tag after --repeat 2      # 고친 뒤
  uv run python -m eval.run_lang --compare before after      # 비교표(eval/results/lang_compare.md)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from apps.api import gemini
from apps.api.langcheck import lang_ok

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
CANDS = json.loads((ROOT / "data" / "lang_candidates.json").read_text(encoding="utf-8"))
LOST_DATE = "2026-11-13"  # 후보 습득일(11/14) 하루 전
QUERIES = {
    "en": "I lost my black leather wallet on subway line 2 yesterday. It has a few credit cards in it.",
    "ja": "昨日、地下鉄2号線で黒い革の財布をなくしました。クレジットカードが数枚入っています。",
    "zh-CN": "昨天在地铁2号线丢了一个黑色皮钱包，里面有几张信用卡。",
    "zh-TW": "昨天在地鐵2號線弄丟了黑色皮夾，裡面有幾張信用卡。",
    "vi": "Hôm qua tôi làm mất ví da màu đen trên tàu điện ngầm tuyến 2. Bên trong có vài thẻ tín dụng.",
    "th": "เมื่อวานฉันทำกระเป๋าสตางค์หนังสีดำหายในรถไฟใต้ดินสาย 2 ข้างในมีบัตรเครดิตหลายใบ",
}
FIELDS = (("name_en", "물품명"), ("desc_en", "설명"), ("reason", "판정 이유"))


def _run_once(lang: str) -> list[dict]:
    """지금 코드의 판정 경로 그대로. 고친 뒤엔 graph.grade_candidates(판정+번역+언어 검사), 그 전엔 gemini.grade."""
    cands = [dict(c) for c in CANDS]
    try:
        from apps.agent.graph import grade_candidates
    except ImportError:  # 고치기 전 코드
        res = {int(x["id"]): x for x in gemini.grade(QUERIES[lang], cands, lang)}
        for i, c in enumerate(cands):
            g = res.get(i) or {}
            c.update({"grade": g.get("score"), "reason": g.get("reason"),
                      "name_en": g.get("name_en"), "desc_en": g.get("desc_en")})
        return cands
    return grade_candidates(QUERIES[lang], cands, lang, lost_date=LOST_DATE)


def measure(tag: str, repeat: int) -> dict:
    tokens = {"calls": 0, "in": 0, "out": 0}
    orig = gemini._meter

    def meter(res):  # 토큰 실측(로컬 통계 DB엔 안 씀)
        u = res.get("usageMetadata") if isinstance(res, dict) else None
        if u:
            tokens["calls"] += 1
            tokens["in"] += u.get("promptTokenCount", 0)
            tokens["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)

    gemini._meter = meter
    out: dict = {"tag": tag, "repeat": repeat, "langs": {}, "tokens": tokens, "fixes": []}
    orig_fix = getattr(gemini, "fix_language", None)
    if orig_fix:  # 결과 언어 검사에 걸린 값과 다시 쓴 결과를 남김(무엇이 섞였고 어떻게 고쳐졌는지)
        def fix(values, lang, skip_prefix=None):
            res = orig_fix(values, lang, skip_prefix)
            for k, v in values.items():
                if res.get(k) != v:
                    out["fixes"].append({"lang": lang, "field": k, "before": v, "after": res.get(k)})
            return res
        gemini.fix_language = fix
    try:
        for lang in QUERIES:
            runs = []
            for _ in range(repeat):
                t0 = time.time()
                cands = _run_once(lang)
                runs.append({"sec": round(time.time() - t0, 1), "items": [
                    {"name": c.get("name"), "grade": c.get("grade"),
                     **{k: c.get(k) for k, _ in FIELDS}} for c in cands]})
            out["langs"][lang] = runs
            print(f"  {lang}: {summary_line(runs, lang)}", flush=True)
    finally:
        gemini._meter = orig
        if orig_fix:
            gemini.fix_language = orig_fix
    OUT.mkdir(exist_ok=True)
    (OUT / f"lang_{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def stats(runs: list[dict], lang: str) -> dict:
    """칸별 (정상, 다른 언어 섞임, 비어서 원문 표시, 전체)."""
    s = {}
    for k, _ in FIELDS:
        ok = bad = empty = 0
        for r in runs:
            for it in r["items"]:
                v = it.get(k)
                if not v:
                    empty += 1
                elif lang_ok(v, lang):
                    ok += 1
                else:
                    bad += 1
        s[k] = (ok, bad, empty, ok + bad + empty)
    return s


def summary_line(runs: list[dict], lang: str) -> str:
    s = stats(runs, lang)
    return " · ".join(f"{label} {s[k][0]}/{s[k][3]} 정상(섞임 {s[k][1]}, 빈칸 {s[k][2]})" for k, label in FIELDS)


def compare(a: str, b: str) -> str:
    ra = json.loads((OUT / f"lang_{a}.json").read_text(encoding="utf-8"))
    rb = json.loads((OUT / f"lang_{b}.json").read_text(encoding="utf-8"))
    lines = [f"# 신고 결과 번역 — 수정 전({a}) / 후({b})", "",
             f"같은 분실 신고(검정 가죽 지갑, 지하철 2호선, 어제)를 6개 언어로 넣고 운영 실제 습득물 12건을 판정. 언어당 {ra['repeat']}회.",
             "검사 = `apps/api/langcheck.lang_ok`(한글 잔존·목표 문자 체계·영어 문장 여부). 칸마다 '정상 / 다른 언어 섞임 / 빈칸(원문 표시)'.", "",
             "| 언어 | 칸 | 수정 전 정상 | 수정 전 섞임 | 수정 후 정상 | 수정 후 섞임 | 수정 후 빈칸(원문) |",
             "|---|---|---|---|---|---|---|"]
    for lang in QUERIES:
        sa, sb = stats(ra["langs"][lang], lang), stats(rb["langs"][lang], lang)
        for k, label in FIELDS:
            (oa, ba, _, ta), (ob, bb, eb, tb) = sa[k], sb[k]
            lines.append(f"| {lang} | {label} | {oa}/{ta} | {ba} | {ob}/{tb} | {bb} | {eb} |")
    for tag, r in ((a, ra), (b, rb)):
        t = r["tokens"]
        secs = [x["sec"] for runs in r["langs"].values() for x in runs]
        lines.append(f"\n- {tag}: AI 호출 {t['calls']}회 · 입력 {t['in']:,} · 출력 {t['out']:,} 토큰, "
                     f"판정 1회 평균 {sum(secs) / len(secs):.1f}초")
    fixes = rb.get("fixes") or []
    if fixes:
        lines += ["", f"## 결과 언어 검사에 걸려 다시 쓴 값({b}, {len(fixes)}칸)", "",
                  "| 언어 | 칸 | 처음 나온 값 | 다시 쓴 뒤(빈칸 = 원문 표시·근거 생략) |", "|---|---|---|---|"]
        for f in fixes[:20]:
            lines.append(f"| {f['lang']} | {f['field'].split(':')[0]} | {f['before'][:70]} | {(f['after'] or '')[:70]} |")
    lines += ["", "## 예시 (각 언어 첫 회, 상위 3건)", ""]
    for lang in QUERIES:
        for tag, r in ((a, ra), (b, rb)):
            items = sorted(r["langs"][lang][0]["items"], key=lambda x: -(x.get("grade") or -1))[:3]
            lines.append(f"**{lang} · {tag}**")
            for it in items:
                lines.append(f"- {it['name']} → {it.get('name_en') or '(원문)'} / {it.get('desc_en') or '-'} / "
                             f"{it.get('grade')}점 — {it.get('reason') or '-'}")
            lines.append("")
    md = "\n".join(lines)
    (OUT / "lang_compare.md").write_text(md, encoding="utf-8")
    return md


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    if args.compare:
        print(compare(*args.compare))
    else:
        measure(args.tag, args.repeat)


if __name__ == "__main__":
    main()
