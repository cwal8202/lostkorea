"""임베딩 차원·모델·결합 방식 비교 — 늦게 등록된 습득물까지 담을 때 서버 메모리를 줄일 수 있는지(2026-11-13).

비교:
- gemini-embedding-001 768차원(현재 운영) / 같은 벡터를 512·256차원으로 자른 것(앞부분 + 다시 정규화)
- gemini-embedding-2 768·512·256차원(001과 벡터 공간이 달라 코퍼스 전부 새로 임베딩)
- 키워드(BM25 char-bigram) + 의미(001·768) RRF 결합(k=60) — 9/8엔 점수 가중합으로 재서 졌음, 이번엔 순위 결합만 추가 확인
질의: 골든셋 v1 한국어 29 + 같은 질의의 영어·일본어·중국어(golden_xlang.json) = 116.
001을 앞부분만 잘라도 되는지: 001에 256차원을 직접 요청한 벡터와 768에서 잘라 정규화한 벡터의 코사인을 비교.

캐시: 001 코퍼스는 기존 emb_cache.json 재사용, 그 밖(질의·Embedding 2 코퍼스)은 emb_dims_cache.json(커밋 안 함).
실행: uv run python eval/run_dims.py
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_baseline import BM25, tokenize  # noqa: E402

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "data" / "corpus.json"
GOLDEN = ROOT / "golden" / "golden.json"
XLANG = ROOT / "golden" / "golden_xlang.json"
OLD_CACHE = ROOT / "data" / "emb_cache.json"          # 001·768 코퍼스(atcId 키)
CACHE = ROOT / "data" / "emb_dims_cache.json"         # "모델|차원|문장" 키
BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODELS = ("gemini-embedding-001", "gemini-embedding-2")
DIMS = (768, 512, 256)


def api_key() -> str:
    for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("GEMINI_API_KEY 없음")


KEY = api_key()


def embed(model: str, texts: list[str], dim: int, task: str = "") -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        part = texts[i : i + 100]
        body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                              "outputDimensionality": dim, **({"taskType": task} if task else {})} for t in part]}
        for attempt in range(8):
            req = urllib.request.Request(f"{BASE}/{model}:batchEmbedContents", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "x-goog-api-key": KEY})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    out += [e["values"] for e in json.load(r)["embeddings"]]
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 503) and attempt < 7:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise SystemExit(f"{model} 임베딩 실패: HTTP {e.code} {e.read()[:300]!r}")
    return out


def norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cut(v: list[float], d: int) -> list[float]:
    return norm(v[:d])


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def ranks_dense(qv: list[float], mat: list[list[float]]) -> list[int]:
    scores = [dot(qv, m) for m in mat]
    return sorted(range(len(mat)), key=lambda i: -scores[i])


def metrics(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    return {"R@1": sum(1 for r in ranks if r is not None and r <= 1) / n,
            "R@5": sum(1 for r in ranks if r is not None and r <= 5) / n,
            "R@10": sum(1 for r in ranks if r is not None and r <= 10) / n,
            "MRR": sum(1 / r for r in ranks if r) / n}


def main() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    ids = [it["atcId"] for it in corpus]
    doc_txt = [f"{it['name']}. 분류:{it['cat']}. 색상:{it['color']}" for it in corpus]  # 운영과 같은 공식
    gold = json.loads(GOLDEN.read_text(encoding="utf-8"))["queries"]
    xl = {q["id"]: q for q in json.loads(XLANG.read_text(encoding="utf-8"))["queries"]}
    queries = [(q["id"], "ko", q["query"], q["expected"]) for q in gold]
    for q in gold:
        for lang in ("en", "ja", "zh"):
            queries.append((q["id"], lang, xl[q["id"]][lang], q["expected"]))
    cache: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    old = json.loads(OLD_CACHE.read_text(encoding="utf-8"))

    def vecs(model: str, texts: list[str], dim: int = 768, task: str = "") -> list[list[float]]:
        k = (lambda t: f"{model}|{dim}|{task}|{t}") if task else (lambda t: f"{model}|{dim}|{t}")
        todo = [t for t in dict.fromkeys(texts) if k(t) not in cache]
        if todo:
            print(f"  {model}·{dim}{'·' + task if task else ''}: {len(todo)}건 임베딩", flush=True)
            for t, v in zip(todo, embed(model, todo, dim, task)):
                cache[k(t)] = [round(x, 6) for x in v]
            CACHE.write_text(json.dumps(cache), encoding="utf-8")
        return [cache[k(t)] for t in texts]

    # 코퍼스·질의 벡터(768로 받아 두고 잘라 씀)
    missing = [i for i, a in enumerate(ids) if a not in old]
    docs = {"gemini-embedding-001": [old.get(a) for a in ids]}
    if missing:
        got = vecs("gemini-embedding-001", [doc_txt[i] for i in missing])
        for i, v in zip(missing, got):
            docs["gemini-embedding-001"][i] = v
    docs["gemini-embedding-2"] = vecs("gemini-embedding-2", doc_txt)
    qtexts = [q[2] for q in queries]
    qv = {m: vecs(m, qtexts) for m in MODELS}

    # 001: 앞부분 자르기 = 256차원 직접 요청과 같은가
    sample = doc_txt[:20]
    direct = vecs("gemini-embedding-001", sample, 256)
    full = vecs("gemini-embedding-001", sample, 768)
    sims = [dot(norm(a), cut(b, 256)) for a, b in zip(direct, full)]
    print(f"\n001 256차원 직접 요청 vs 768에서 자르기 — 코사인 평균 {sum(sims)/len(sims):.4f} (최소 {min(sims):.4f})")
    direct2 = vecs("gemini-embedding-2", sample, 256)
    full2 = vecs("gemini-embedding-2", sample, 768)
    sims2 = [dot(norm(a), cut(b, 256)) for a, b in zip(direct2, full2)]
    print(f"Embedding 2 같은 비교 — 코사인 평균 {sum(sims2)/len(sims2):.4f} (최소 {min(sims2):.4f})")

    pos = {a: i for i, a in enumerate(ids)}
    results: dict[str, dict] = {}
    per_query: dict[str, list] = {}
    for m in MODELS:
        for d in DIMS:
            mat = [cut(v, d) for v in docs[m]]
            rk = []
            for (qid, lang, text, exp), v in zip(queries, qv[m]):
                order = ranks_dense(cut(v, d), mat)
                rk.append((lang, order.index(pos[exp]) + 1))
            name = f"{m.replace('gemini-embedding-', 'G')}·{d}"
            results[name] = {lang: metrics([r for lg, r in rk if lg == lang]) for lang in ("ko", "en", "ja", "zh")}
            results[name]["all"] = metrics([r for _, r in rk])
            per_query[name] = rk

    # 검색용 작업 지정(taskType) — 모델에 따라 지정해야 제 성능이 나올 수 있어 두 모델 모두 공정하게
    for m in MODELS:
        dmat = [cut(v, 768) for v in vecs(m, doc_txt, 768, "RETRIEVAL_DOCUMENT")]
        qq = vecs(m, qtexts, 768, "RETRIEVAL_QUERY")
        rk = [(lang, ranks_dense(cut(v, 768), dmat).index(pos[exp]) + 1)
              for (qid, lang, text, exp), v in zip(queries, qq)]
        name = f"{m.replace('gemini-embedding-', 'G')}·768·task"
        results[name] = {lang: metrics([r for lg, r in rk if lg == lang]) for lang in ("ko", "en", "ja", "zh")}
        results[name]["all"] = metrics([r for _, r in rk])

    # 7비트 스칼라 압축 흉내(차원별 최소~최대를 128칸으로) — OpenSearch Lucene SQ와 같은 원리의 근사
    def sq7(mat: list[list[float]], lo: list[float], hi: list[float]) -> list[list[float]]:
        return [norm([lo[j] + round((x - lo[j]) / ((hi[j] - lo[j]) or 1) * 127) / 127 * (hi[j] - lo[j])
                      for j, x in enumerate(v)]) for v in mat]
    for d in (768, 512):
        mat = [cut(v, d) for v in docs["gemini-embedding-001"]]
        lo = [min(v[j] for v in mat) for j in range(d)]
        hi = [max(v[j] for v in mat) for j in range(d)]
        qmat = sq7(mat, lo, hi)
        rk = []
        for (qid, lang, text, exp), v in zip(queries, qv["gemini-embedding-001"]):
            q = sq7([[min(max(x, lo[j]), hi[j]) for j, x in enumerate(cut(v, d))]], lo, hi)[0]
            rk.append((lang, ranks_dense(q, qmat).index(pos[exp]) + 1))
        name = f"G001·{d}·SQ7"
        results[name] = {lang: metrics([r for lg, r in rk if lg == lang]) for lang in ("ko", "en", "ja", "zh")}
        results[name]["all"] = metrics([r for _, r in rk])

    # RRF: BM25(char-bigram) + 001·768 — 한국어 질의만(외국어는 BM25가 거의 못 맞힘)
    bm = BM25([tokenize(t) for t in doc_txt])
    mat = [cut(v, 768) for v in docs["gemini-embedding-001"]]
    rrf_ranks = []
    for (qid, lang, text, exp), v in zip(queries, qv["gemini-embedding-001"]):
        if lang != "ko":
            continue
        dense = ranks_dense(cut(v, 768), mat)
        bm_rank = {i: r for r, (i, _) in enumerate(bm.search(tokenize(text)), 1)}
        score = {i: 1 / (60 + r) for r, i in enumerate(dense, 1)}
        for i, r in bm_rank.items():
            score[i] = score.get(i, 0) + 1 / (60 + r)
        order = sorted(score, key=lambda i: -score[i])
        rrf_ranks.append(order.index(pos[exp]) + 1)
    results["RRF(BM25+G001·768)"] = {"ko": metrics(rrf_ranks)}

    print(f"\n코퍼스 {len(ids)}건 / 질의 {len(queries)}개(한국어 29 + 영·일·중 각 29)\n")
    print(f"{'방식':<20}" + "".join(f"{lang + ' MRR':>10}{lang + ' R@1':>9}" for lang in ("ko", "en", "ja", "zh", "all")))
    for name, r in results.items():
        row = f"{name:<20}"
        for lang in ("ko", "en", "ja", "zh", "all"):
            x = r.get(lang)
            row += f"{x['MRR']:>10.3f}{x['R@1']:>9.3f}" if x else f"{'-':>10}{'-':>9}"
        print(row)
    print("\n질의별 순위가 기준(G001·768)보다 나빠진 것(한국어):")
    base = dict(enumerate(r for lg, r in per_query["G001·768"]))
    for name, rk in per_query.items():
        worse = [(queries[i][0], queries[i][1], base[i], r) for i, (lg, r) in enumerate(rk) if r > base[i] and lg == "ko"]
        if worse and name != "G001·768":
            print(f"  {name}: " + ", ".join(f"{q}({b}→{r}위)" for q, _, b, r in worse))
    print("\n메모리 어림(공식 1.1×(벡터 바이트 + 8×m), m=16) — 18만 / 50만 건:")
    for d in DIMS:
        for kind, b in (("float", 4 * d), ("SQ 7bit", d)):
            per = 1.1 * (b + 8 * 16)
            print(f"  {d}차원 {kind:<8}: {per * 181_874 / 2**30:5.2f}GB / {per * 500_000 / 2**30:5.2f}GB")


if __name__ == "__main__":
    main()
