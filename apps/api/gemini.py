"""Gemini 호출 — 질의 임베딩 + LLM grading. urllib(stdlib)만 사용."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

from .config import settings

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 프롬프트는 스캐폴드 단계라 여기 둠 — 앱 확장 시 apps/agent/prompts/ 로 이동 예정.
_GRADE_PROMPT = """{lang_rule}당신은 분실물↔습득물 매칭 심판입니다.
사용자가 잃어버린 물건 설명과, 검색이 좁힌 습득물 후보 목록이 주어집니다.
각 후보가 '사용자가 잃어버린 바로 그 물건'일 가능성을 0~100으로 채점하세요.

원칙:
- 브랜드·모델·물품종류·색상 일치를 중시. 한글↔영문 브랜드(닥스=DAKS)는 같게 봄.
- 특이사항(내용물·표시)이 사용자 설명과 맞으면 올리고, 어긋나면 내림.
- 후보에 정보가 적으면 과신하지 말 것.
- 지역은 참고만(보관 장소는 습득 장소와 다를 수 있음).
- {date_rule}
- 근거(reason)는 후보에 실제로 적힌 사실로만 한 줄. 없는 특징을 지어내지 말 것.

반드시 아래 JSON 배열로만 답하세요(설명 문장 금지):
[{{"id": <후보번호>, "score": <0~100 정수>, "reason": "<한 줄>"}}, ...]

잃어버린 물건: "{query}"

후보:
{candidates}
"""

# 판정 이유 예시(신고자 언어) — 예시가 영어뿐이면 일본어·베트남어 신고에도 영어가 섞여 나왔음(11/15 측정)
_REASON_EXAMPLE = {
    "en": "Same type and color (black leather wallet); brand not stated.",
    "ja": "黒い革の財布で種類と色が一致。ブランドの記載なし。",
    "zh-CN": "黑色皮夹，种类和颜色一致；未注明品牌。",
    "zh-TW": "黑色皮夾，種類與顏色相符；未註明品牌。",
    "vi": "Cùng loại ví da màu đen; không ghi thương hiệu.",
    "th": "กระเป๋าสตางค์หนังสีดำ ประเภทและสีตรงกัน ไม่ระบุยี่ห้อ",
}


def _lang_rule(lang: str) -> str:
    """프롬프트 맨 앞에 둘 출력 언어 규칙(한국어면 없음). 예전엔 끝에 덧붙여 근거가 한국어로 섞였음."""
    if lang == "ko":
        return ""
    lname = lang_name(lang)
    ex = _REASON_EXAMPLE.get(lang) or _REASON_EXAMPLE.get(lang.split("-")[0])
    return (f"[출력 언어] 'reason'은 반드시 자연스러운 {lname}로만 쓰세요. 한국어 낱말·한글을 섞지 말 것 — "
            f"아래 후보 정보의 한국어 낱말(예: 습득, 기차, 내용물)도 {lname}로 옮겨 쓰고, "
            f"한국 브랜드·카드사·역 이름만 로마자로(예: 신한 → Shinhan, 서울역 → Seoul Station)."
            + (" 베트남어 성조·모음 부호를 빠뜨리지 말 것." if lang.startswith("vi") else "")
            + (f' 예: "{ex}"' if ex else "") + "\n\n")


def _date_rule(lost_date: str | None, today: str) -> str:
    """분실일이 있으면 날짜 모순을 점수에 반영. 검색이 이미 분실일 전날 이후만 가져오지만, 저장된 후보 재판정도 같은 기준으로."""
    if not lost_date:
        return f"날짜는 참고만(오늘 {today})."
    return (f"분실일 {lost_date}(오늘 {today}). 습득일이 분실일보다 이르면 같은 물건일 가능성이 낮으니 낮게 "
            f"(하루 이른 건 날짜 착오일 수 있어 조금만). 습득일이 분실일과 같거나 늦으면 날짜는 문제없음.")


def _cand_line(n: int, c: dict) -> str:
    from .desc import clean_desc

    note = clean_desc(c.get("description"))[:100] or "없음"
    return (f"{n}. 물품명:{c.get('name', '')} / 제목:{c.get('subject', '')} / 분류:{c.get('category', '')} / "
            f"색상:{c.get('color', '')} / 특이사항:{note} / 습득장소:{c.get('found_place') or '미상'} / "
            f"지역:{c.get('region') or '미상'} / 습득일:{c.get('found_at', '')}")


_ACTIVE: dict[str, str | None] = {"model": None}  # 404로 예비 모델로 바뀌면 기록(프로세스가 살아 있는 동안 유지)


def _model() -> str:
    return _ACTIVE["model"] or settings.gemini_grade_model


def _gen_url() -> str:
    return f"{_BASE}/{_model()}:generateContent"


_THINK_LEVELS = ("minimal", "low")  # 낮은 순. 모델마다 받는 수준이 다름(실측: 3.6-flash는 minimal 가능, 3.8-flash는 400)
_THINK_OK: dict[str, str] = {}      # 모델별로 실제 받아준 수준("" = 설정을 안 보냄) — 거부당하면 기록


def _gen_config() -> dict:
    """생성 호출 공통 설정 — JSON 응답, 결정적(temperature 0), thinking 수준(비용의 대부분 — config 참고)."""
    cfg: dict = {"responseMimeType": "application/json", "temperature": 0}
    level = _THINK_OK.get(_model(), settings.gemini_thinking_level)
    if level:
        cfg["thinkingConfig"] = {"thinkingLevel": level}
    return cfg


def _step_down_thinking(url: str, body: dict, message: str) -> bool:
    """모델이 thinking 수준을 거부(400) → 한 단계 위 수준, 그래도 안 되면 설정 생략. 모델별로 기억.

    모델을 바꾸거나 예비 모델로 전환됐을 때 thinking 설정 하나 때문에 모든 AI 호출이 실패하는 것을 막는다.
    """
    gc = body.get("generationConfig") or {}
    tc = gc.get("thinkingConfig")
    if not tc or "thinking" not in message.lower():
        return False
    cur = tc.get("thinkingLevel", "")
    i = _THINK_LEVELS.index(cur) if cur in _THINK_LEVELS else len(_THINK_LEVELS)
    nxt = _THINK_LEVELS[i + 1] if i + 1 < len(_THINK_LEVELS) else ""
    _THINK_OK[url.rsplit("/", 1)[-1].split(":")[0]] = nxt
    if nxt:
        gc["thinkingConfig"] = {"thinkingLevel": nxt}
    else:
        gc.pop("thinkingConfig", None)
    return True


def _switch_to_fallback(reason: str = "gone") -> bool:
    """기본 모델을 못 쓸 때 예비 모델로 전환 + 관리자 알림(1회). 이미 예비 모델이면 False.

    reason: 'gone' = 404(모델 종료·차단) / 'daily' = 하루 요청 한도 초과(429, 모델별 한도라 예비 모델은 따로 남아 있음).
    """
    fb = settings.gemini_fallback_model
    if not fb or _model() == fb:
        return False
    old = _model()
    _ACTIVE["model"] = fb
    if reason == "daily":
        body = (f"{old}의 하루 요청 한도(모델별)를 넘어 {fb}로 자동 전환했어요. 서비스는 계속 동작합니다(예비 모델은 단가가 더 높음).\n"
                f"한도는 태평양 자정(한국 오후 5시 무렵)에 풀립니다. 그 뒤 backend를 재시작하면 다시 {old}부터 씁니다.")
    else:
        body = (f"{old} 호출이 404(모델 종료·차단)로 실패해 {fb}로 자동 전환했어요. 서비스는 계속 동작합니다.\n"
                f".env의 GEMINI_GRADE_MODEL을 {fb}(또는 새 모델)로 바꾸고 재배포하세요. 재시작하면 다시 {old}부터 시도합니다.")
    try:
        import threading

        from apps import ops
        threading.Thread(target=ops.alert, daemon=True, args=(f"Gemini 모델 전환 — {old} → {fb}", body)).start()
    except Exception:  # noqa: BLE001 — 알림 실패가 전환을 막지 않게
        pass
    return True


def _daily_quota(err: str) -> bool:
    """429가 '하루 한도'인가(분당 한도는 잠깐 기다리면 풀리니 전환하지 않음)."""
    return "PerDay" in err or "per_day" in err


def _post(url: str, body: dict, timeout: int = 60, max_retry: int = 3) -> dict:
    """429/5xx는 백오프 재시도. 기본 3회(사용자 대기 중인 요청) — 대량 배치 임베딩만 더 길게.

    API 키는 URL 쿼리가 아니라 `x-goog-api-key` 헤더로(오류 로그·추적에 URL이 찍혀도 키가 남지 않게).
    """
    headers = {"Content-Type": "application/json", "x-goog-api-key": settings.gemini_api_key}
    attempt = 0  # 일시 오류(429·5xx·네트워크) 재시도 횟수 — 모델 전환·thinking 조정은 세지 않음(각각 유한)
    while True:
        try:
            req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.load(r)
            _meter(res)
            return res
        except urllib.error.HTTPError as e:
            if e.code == 404 and ":generateContent" in url and _switch_to_fallback():
                url = _gen_url()  # 모델이 사라짐 → 예비 모델로 즉시 재시도
                continue
            if e.code == 429 and ":generateContent" in url and _daily_quota(_err_text(e)) and _switch_to_fallback("daily"):
                url = _gen_url()  # 하루 한도 초과(모델별) → 예비 모델로 즉시 재시도
                continue
            if e.code == 400 and _step_down_thinking(url, body, _err_text(e)):
                continue          # 이 모델이 thinking 수준을 거부 → 조정해 즉시 재시도
            attempt += 1
            if e.code in (429, 500, 503) and attempt < max_retry:
                time.sleep(min(30, 5 * attempt))  # 5,10,...,30s
                continue
            raise
        except OSError:  # URLError·타임아웃·연결 끊김(HTTPError는 위에서 처리)
            attempt += 1
            if attempt < max_retry:
                time.sleep(5)
                continue
            raise


def _meter(res: dict) -> None:
    """생성 호출의 실제 토큰(usageMetadata)을 일별 통계에 — 주간 요약에서 실사용·비용 확인. 실패해도 응답엔 영향 없음."""
    u = res.get("usageMetadata") if isinstance(res, dict) else None
    if not u:
        return
    try:
        from apps import store
        store.bump_many({"ai_calls": 1, "ai_in": u.get("promptTokenCount", 0),
                         "ai_out": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)})
    except Exception:  # noqa: BLE001
        pass


def _err_text(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:2000]
    except Exception:  # noqa: BLE001
        return ""


def embed_query(text: str) -> list[float]:
    """질의 → 정규화된 768-dim 벡터 (batchEmbedContents 1건)."""
    url = f"{_BASE}/{settings.gemini_embed_model}:batchEmbedContents"
    body = {
        "requests": [{
            "model": f"models/{settings.gemini_embed_model}",
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": settings.gemini_embed_dim,
        }]
    }
    v = _post(url, body)["embeddings"][0]["values"]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def generate_json(prompt: str, timeout: int = 60):
    """gemini-3.6-flash로 JSON 응답 생성(추출·라우팅 등 재사용). 실패 시 None."""
    url = _gen_url()
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": _gen_config(),
    }
    try:
        text = _post(url, body, timeout)["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError, OSError, RuntimeError):
        return None


def generate_json_image(prompt: str, image_b64: str, mime: str = "image/jpeg", timeout: int = 60):
    """사진 + 프롬프트 → JSON (Vision 추출용). gemini-3.6-flash 멀티모달. 실패 시 None."""
    url = _gen_url()
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": image_b64}},
            {"text": prompt},
        ]}],
        "generationConfig": _gen_config(),
    }
    try:
        text = _post(url, body, timeout)["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError, OSError, RuntimeError):
        return None


_COMPARE_PROMPT = """두 이미지가 '같은(또는 매우 비슷한) 물건'인지 판단하세요.
첫 번째는 사용자가 잃어버린 물건 사진, 두 번째는 습득물 보관 사진입니다.
색상·형태·재질·브랜드·종류를 종합해, 같은 물건일 가능성을 0~100으로 채점하세요.
습득물 사진은 조명·각도·배경이 다를 수 있으니 그 점은 감안하세요.
반드시 아래 JSON만: {"score": <0~100 정수>, "reason": "<한 줄 근거>"}"""


def compare_images(user_b64: str, cand_b64: str, user_mime: str = "image/jpeg",
                   cand_mime: str = "image/jpeg", lang: str = "ko") -> dict | None:
    """사용자 사진 ↔ 습득물 사진 직접 대조(멀티모달). {"score","reason"} 또는 None(실패)."""
    prompt = _COMPARE_PROMPT
    if lang != "ko":
        prompt += f"\n중요: 'reason'은 {lang_name(lang)}로 작성하세요."
    url = _gen_url()
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": user_mime, "data": user_b64}},
            {"inline_data": {"mime_type": cand_mime, "data": cand_b64}},
        ]}],
        "generationConfig": _gen_config(),
    }
    try:
        text = _post(url, body, timeout=60)["candidates"][0]["content"]["parts"][0]["text"]
        r = json.loads(text)
        return {"score": int(r.get("score")), "reason": (r.get("reason") or "").strip()}
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError, OSError, RuntimeError):
        return None


def embed_texts(texts: list[str], chunk: int = 50) -> list[list[float]]:
    """여러 문서를 배치 임베딩(정규화). 수집기 색인용. batchEmbedContents 청크."""
    out: list[list[float]] = []
    url = f"{_BASE}/{settings.gemini_embed_model}:batchEmbedContents"
    n_chunks = (len(texts) + chunk - 1) // chunk
    for idx, i in enumerate(range(0, len(texts), chunk)):
        part = texts[i : i + chunk]
        body = {"requests": [{
            "model": f"models/{settings.gemini_embed_model}",
            "content": {"parts": [{"text": t}]},
            "outputDimensionality": settings.gemini_embed_dim,
        } for t in part]}
        for e in _post(url, body, timeout=120, max_retry=6)["embeddings"]:  # 수집 배치: 쿼터 대비 길게
            v = e["values"]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        if idx < n_chunks - 1:
            time.sleep(0.6)  # 청크 간 페이싱(RPM 한도 여유)
    return out


_LANG_NAME = {  # 웹 번역 위젯이 제공하는 13개 언어 + 한국어
    "ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese",
    "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian", "ms": "Malay", "hi": "Hindi", "fr": "French", "es": "Spanish", "ru": "Russian",
    "de": "German",
}


def normalize_lang(code: str | None) -> str:
    """사용자 언어 코드 정리: 'en-US'→'en', 'zh-cn'→'zh-CN'. 형식이 이상하면 'ko'(프롬프트 주입 방지)."""
    code = (code or "").strip()
    if not code or len(code) > 10 or not all(ch.isalpha() or ch == "-" for ch in code):
        return "ko"
    base, _, region = code.partition("-")
    base = base.lower()
    if base == "zh":
        return "zh-TW" if region.upper() in ("TW", "HK", "MO") else "zh-CN"
    return base


def lang_name(code: str) -> str:
    return _LANG_NAME.get(code) or _LANG_NAME.get(code.split("-")[0]) or f"the language '{code}'"


_TRANSLATE_PROMPT = """아래 JSON의 각 값(한국어)을 {lname}로 번역하세요. 분실물 알림 메일에 들어갈 문구입니다.
규칙:
- 키는 그대로, 값만 번역. JSON 객체 하나만 출력.
- {{t}} {{n}} {{url}} 같은 중괄호 자리표시자, URL, 숫자, 이모지는 바꾸지 말 것.
- 키가 "place_"로 시작하면 장소·기관 이름: "한국어 원문 ({lname} 번역 또는 로마자 표기)" 형식으로
  (예: "강남경찰서 (Gangnam Police Station)"). 보관기관에 그대로 보여줄 수 있게 한국어 원문을 반드시 유지.
- 빈 값은 빈 값으로.

{payload}"""


def translate_strings(strings: dict[str, str], lang: str) -> dict[str, str] | None:
    """한국어 문자열 dict → 사용자 언어 dict(키 유지). 실패·누락 시 None(호출부가 폴백).

    결과 언어 검사: 목표 언어가 아닌 값(장소 'place_' 칸은 한국어 원문을 일부러 남기므로 제외)은 한 번 다시 쓰게 하고,
    그래도 아니면 그 키만 뺀다 — 호출부가 원문으로 폴백(고정 문구 표는 키가 하나라도 빠지면 영어 표로).
    """
    if not strings:
        return {}
    prompt = _TRANSLATE_PROMPT.format(lname=lang_name(lang),
                                      payload=json.dumps(strings, ensure_ascii=False, indent=1))
    res = generate_json(prompt)
    if not isinstance(res, dict) or not all(isinstance(res.get(k), str) for k in strings):
        return None
    out = {k: res[k] for k in strings}
    return fix_language(out, lang, skip_prefix="place_")


_ITEMS_PROMPT = """[출력 언어: {lname}] 한국어를 그대로 두지 말고 모든 값을 {lname}로 쓰세요.

한국 습득물의 물품명과 특이사항을 {lname}로 번역하세요. 분실물 찾기 결과 화면·메일에 그대로 보여 줄 문구입니다.
규칙:
- 값은 {lname}로만. 한글을 남기지 말 것 — 한국 브랜드·카드사·은행·교통카드 이름은 로마자나 {lname} 표기로
  (예: 신한 → Shinhan, 국민 → KB Kookmin, 카뱅 → KakaoBank, 티머니 → T-money, 루이까또즈 → Louis Quatorze).
- 번역만 하세요. 판단·비교·추측을 덧붙이지 말 것(원문에 없는 색·브랜드·장소·특징 금지).
- 숫자·모델명·영문 표기는 그대로. 특이사항이 빈칸이면 "desc"도 빈칸. 베트남어면 성조·모음 부호를 빠뜨리지 말 것.
- 분류는 뜻을 고르는 맥락일 뿐(번역해서 넣지 말 것).
반드시 JSON 배열로만: [{{"i": <번호>, "name": "<물품명>", "desc": "<특이사항>"}}, ...]

{lines}
"""


def translate_items(items: list[dict], lang: str) -> dict[int, dict]:
    """후보 습득물의 물품명·특이사항(상투 문장 제외)을 신고자 언어로 — 한 번의 호출. {번호: {"name","desc"}}. 실패 시 {}.

    판정과 따로(동시에) 부른다 — 실제 원문을 번역하므로 화면의 '설명'이 지어낸 문장이 아니라 실제 특이사항이 됨.
    """
    from .desc import clean_desc

    if not items or lang == "ko":
        return {}
    lines = "\n".join(f"{i}. 분류:{c.get('category', '')} / 물품명:{c.get('name', '')} / "
                      f"특이사항:{clean_desc(c.get('description'))[:160]}" for i, c in enumerate(items))
    res = generate_json(_ITEMS_PROMPT.format(lname=lang_name(lang), lines=lines))
    out: dict[int, dict] = {}
    for x in res if isinstance(res, list) else []:
        try:
            i = int(x.get("i"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= i < len(items):
            out[i] = {k: x[k].strip()[:300] for k in ("name", "desc") if isinstance(x.get(k), str) and x[k].strip()}
    return out


_REWRITE_PROMPT = """아래 JSON 값들은 {lname}로 써야 하는데 다른 언어(한국어·영어 등)가 섞였습니다.
뜻은 그대로 두고 값을 {lname}로만 다시 쓰세요. 한글을 남기지 말고, 한국 브랜드·카드사 이름은 로마자로(예: 신한 → Shinhan).
키는 그대로, JSON 객체 하나만.

{payload}"""


def fix_language(values: dict[str, str], lang: str, skip_prefix: str | None = None) -> dict[str, str]:
    """결과 언어 검사 + 한 번만 다시 쓰기. 그래도 목표 언어가 아닌 값은 뺀 dict를 돌려줌(호출부가 원문 표시).

    통계: ai_lang_retry(다시 쓴 칸 수)·ai_lang_fail(끝내 빠진 칸 수) — 주간 요약에서 품질 추이 확인.
    """
    from .langcheck import lang_ok

    if lang == "ko":
        return values
    bad = {k: v for k, v in values.items()
           if v and not (skip_prefix and k.startswith(skip_prefix)) and not lang_ok(v, lang)}
    if not bad:
        return values
    res = generate_json(_REWRITE_PROMPT.format(lname=lang_name(lang),
                                               payload=json.dumps(bad, ensure_ascii=False, indent=1)))
    res = res if isinstance(res, dict) else {}
    out, failed = dict(values), 0
    for k in bad:
        v = res.get(k)
        if isinstance(v, str) and v.strip() and lang_ok(v, lang):
            out[k] = v.strip()
        else:
            out.pop(k)
            failed += 1
    try:
        from apps import store
        store.bump_many({"ai_lang_retry": len(bad), "ai_lang_fail": failed})
    except Exception:  # noqa: BLE001
        pass
    return out


def grade(query: str, candidates: list[dict], lang: str = "ko", lost_date: str | None = None,
          today: str | None = None) -> list[dict]:
    """top-K 후보를 채점. 반환: [{"id":n, "score":0~100, "reason":".."}]. 실패 시 [].

    판정은 점수·근거만 — 물품명·특이사항 번역은 translate_items가 따로(같은 호출에 번역까지 시키면 일본어·베트남어에서
    이름이 영어, 근거가 한국어로 섞였음 — 11/15 측정). lang!="ko"이면 'reason'만 그 언어로. 점수 로직은 언어 무관.
    후보 줄에 특이사항(상투 문장 제외)·습득장소, 프롬프트에 분실일·오늘 날짜를 넣음(예전엔 셋 다 빠져 있었음).
    """
    from datetime import datetime, timedelta, timezone

    today = today or datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    lines = "\n".join(_cand_line(n, c) for n, c in enumerate(candidates))
    prompt = _GRADE_PROMPT.format(lang_rule=_lang_rule(lang), date_rule=_date_rule(lost_date, today),
                                  query=query, candidates=lines)
    url = _gen_url()
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": _gen_config(),
    }
    try:
        text = _post(url, body)["candidates"][0]["content"]["parts"][0]["text"]
        return clean_grades(json.loads(text), len(candidates))
    except (KeyError, IndexError, json.JSONDecodeError, OSError, RuntimeError):
        return []


def clean_grades(raw, n: int) -> list[dict]:
    """LLM 판정 응답 검증 — 리스트·정수 id(0..n-1)·점수 0~100·문자열 근거만 통과(형식 이탈로 요청이 죽지 않게)."""
    if isinstance(raw, dict):  # {"results": [...]}처럼 감싸 온 경우
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    out = []
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        try:
            i, score = int(x.get("id")), int(round(float(x.get("score"))))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < n:
            continue
        item = {"id": i, "score": max(0, min(100, score))}
        for k in ("reason", "name_en", "desc_en"):
            if isinstance(x.get(k), str):
                item[k] = x[k].strip()[:300]
        out.append(item)
    return out
