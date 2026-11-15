"""신고 결과 언어 — 6개 언어(영어·일본어·간체·번체·베트남어·태국어) 회귀 시험.

11/15 측정: 판정 한 번에 이름·설명 번역까지 시키자 일본어는 이름·설명이 영어, 근거가 한국어로, 베트남어·번체는 근거에
한국어 낱말이 섞였다. 지금 구조: 판정(점수·근거)과 번역(실제 원문)을 따로 → 결과 언어 검사 → 한 번 다시 쓰기 → 그래도 아니면 원문.
외부 서비스 없음: Gemini는 전부 목킹.
"""

import json

import pytest

from apps import store
from apps.agent import graph
from apps.api import gemini
from apps.api.langcheck import lang_ok

LANGS = ("en", "ja", "zh-CN", "zh-TW", "vi", "th")
# 언어별 (물품명, 설명, 근거) — 정상 출력
GOOD = {
    "en": ("Black card wallet", "ID card and credit cards inside", "Same black wallet; brand not stated."),
    "ja": ("黒いカードケース", "身分証とクレジットカード入り", "黒い財布で色が一致。ブランドの記載なし。"),
    "zh-CN": ("黑色卡包", "内有身份证和信用卡", "黑色钱包，颜色一致；未注明品牌。"),
    "zh-TW": ("黑色卡夾", "內有身分證和信用卡", "黑色皮夾，顏色相符；未註明品牌。"),
    "vi": ("Ví đựng thẻ màu đen", "Có chứng minh thư và thẻ tín dụng", "Cùng màu đen; không ghi thương hiệu."),
    "th": ("กระเป๋าใส่บัตรสีดำ", "มีบัตรประชาชนและบัตรเครดิต", "สีดำตรงกัน ไม่ระบุยี่ห้อ"),
}
# 언어별 섞인 출력(11/15 실제 측정에서 나온 모양) — 검사에 걸려야 함
MIXED = {
    "en": ["검정 카드지갑", "Color matches, 브랜드 미상."],
    "ja": ["Black Card Wallet", "A black Dior wallet, matching the color and item type.", "색상 일치"],
    "zh-CN": ["颜色一致，브랜드 없음", "黑色錢包"],                 # 한국어 섞임 / 번체 글자
    "zh-TW": ["顏色為棕색，與失主描述的黑色皮夾顏色不符。", "黑色钱包"],  # 한국어 섞임 / 간체 글자
    "vi": ["Màu sắc (nâu) không 일치 với ví đen được 보관.", "Black wallet with cards",
           "La vi the mau đen duoc nhat o xe dien ngam"],                # 성조 부호 빠진 베트남어(수정 후 측정에서 나옴)
    "th": ["Black wallet with cards", "สีดำ 일치"],
}


@pytest.mark.parametrize("lang", LANGS)
def test_lang_check_accepts_target_language(lang):
    assert all(lang_ok(v, lang) for v in GOOD[lang])


@pytest.mark.parametrize("lang", LANGS)
def test_lang_check_catches_mixed_output(lang):
    assert not any(lang_ok(v, lang) for v in MIXED[lang])


def test_lang_check_lets_brand_only_names_through():
    for lang in ("ja", "zh-CN", "zh-TW", "vi", "th"):
        assert lang_ok("iPhone 15 Pro", lang) and lang_ok("AirPods", lang) and lang_ok("DAKS", lang)
    assert not lang_ok("체크카드 (신한)", "ja")          # 한국 브랜드도 한글이면 실패 → 다시 쓰기(Shinhan)
    assert lang_ok("", "ja") and lang_ok("아무거나", "ko")


# ── 판정 프롬프트 ──

def _capture_prompt(monkeypatch) -> list[str]:
    sent = []

    def fake_post(url, body, timeout=60, max_retry=3):
        sent.append(body["contents"][0]["parts"][0]["text"])
        return {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}

    monkeypatch.setattr(gemini, "_post", fake_post)
    return sent


CAND = {"name": "검정카드지갑", "subject": "검정카드지갑(블랙(검정)색)을 습득하여 보관하고 있습니다.",
        "category": "지갑 > 기타 지갑", "color": "블랙(검정)", "found_place": "지하철", "region": "서울특별시 중구",
        "found_at": "2026-11-14",
        "description": "내용 서울역에서는 [2026.11.14] 지갑을 습득/보관 하였습니다. 특이사항 : 신분증, 신한카드 2장"}


@pytest.mark.parametrize("lang", LANGS)
def test_grade_prompt_puts_target_language_first_and_asks_only_score_and_reason(monkeypatch, lang):
    sent = _capture_prompt(monkeypatch)
    gemini.grade("black wallet", [CAND], lang, lost_date="2026-11-13", today="2026-11-15")
    p = sent[0]
    assert p.startswith("[출력 언어]") and gemini.lang_name(lang) in p.split("\n")[0]
    assert gemini._REASON_EXAMPLE[lang] in p                      # 예시도 그 언어로(예전엔 영어 예시뿐)
    assert "name_en" not in p and "desc_en" not in p               # 번역은 판정에서 분리
    assert "특이사항:신분증, 신한카드 2장" in p and "습득장소:지하철" in p   # 실제 단서(상투 문장은 뺌)
    assert "분실일 2026-11-13(오늘 2026-11-15)" in p and "하루 이른 건" in p


def test_grade_prompt_korean_has_no_language_rule_and_soft_date(monkeypatch):
    sent = _capture_prompt(monkeypatch)
    gemini.grade("검정 지갑", [CAND], "ko", today="2026-11-15")
    assert not sent[0].startswith("[출력 언어]") and "날짜는 참고만(오늘 2026-11-15)" in sent[0]


# ── 판정 + 번역 + 언어 검사 ──

@pytest.fixture
def ai(monkeypatch):
    """판정·번역·다시 쓰기 응답을 정하는 가짜 Gemini. calls에 호출 기록."""
    w = {"grade": [], "items": {}, "rewrite": {}, "calls": []}

    def fake_grade(text, cands, lang="ko", lost_date=None, today=None):
        w["calls"].append(("grade", lang, lost_date))
        return w["grade"]

    def fake_items(items, lang):
        w["calls"].append(("items", lang))
        if isinstance(w["items"], Exception):
            raise w["items"]
        return w["items"]

    def fake_json(prompt, timeout=60):
        w["calls"].append(("rewrite", prompt))
        return w["rewrite"]

    monkeypatch.setattr(gemini, "grade", fake_grade)
    monkeypatch.setattr(gemini, "translate_items", fake_items)
    monkeypatch.setattr(gemini, "generate_json", fake_json)
    store.init()
    store._run("DELETE FROM metrics")
    return w


def _two():
    return [{"atc_id": "A", "fd_sn": "1", "name": "검정카드지갑"}, {"atc_id": "B", "fd_sn": "1", "name": "갈색 지갑"}]


@pytest.mark.parametrize("lang", LANGS)
def test_good_output_passes_through_without_rewrite(ai, lang):
    name, desc, reason = GOOD[lang]
    ai["grade"] = [{"id": 0, "score": 85, "reason": reason}, {"id": 1, "score": 40, "reason": reason}]
    ai["items"] = {0: {"name": name, "desc": desc}, 1: {"name": name}}
    out = graph.grade_candidates("q", _two(), lang)
    assert [c["atc_id"] for c in out] == ["A", "B"]
    assert (out[0]["name_en"], out[0]["desc_en"], out[0]["reason"]) == (name, desc, reason)
    assert "desc_en" not in out[1]                                       # 특이사항 없으면 설명도 없음
    assert not any(c[0] == "rewrite" for c in ai["calls"])


@pytest.mark.parametrize("lang", LANGS)
def test_mixed_output_is_rewritten_once(ai, lang):
    name, desc, reason = GOOD[lang]
    bad = MIXED[lang][0]
    ai["grade"] = [{"id": 0, "score": 85, "reason": bad}]
    ai["items"] = {0: {"name": bad, "desc": desc}}
    ai["rewrite"] = {"name_en:0": name, "reason:0": reason}
    out = graph.grade_candidates("q", _two()[:1], lang)
    assert (out[0]["name_en"], out[0]["desc_en"], out[0]["reason"]) == (name, desc, reason)
    rewrites = [c for c in ai["calls"] if c[0] == "rewrite"]
    assert len(rewrites) == 1 and gemini.lang_name(lang) in rewrites[0][1]
    assert json.loads(rewrites[0][1].split("\n\n", 1)[1]) == {"name_en:0": bad, "reason:0": bad}
    m = store.metrics_since("2000-01-01")
    assert m.get("ai_lang_retry") == 2 and not m.get("ai_lang_fail")


@pytest.mark.parametrize("lang", LANGS)
def test_still_mixed_after_rewrite_falls_back_to_original(ai, lang):
    """다시 써도 섞이면 다른 언어를 보여 주지 않음 — 물품명·설명은 한국어 원문, 근거는 생략."""
    bad = MIXED[lang][0]
    ai["grade"] = [{"id": 0, "score": 85, "reason": bad}]
    ai["items"] = {0: {"name": bad, "desc": bad}}
    ai["rewrite"] = {"name_en:0": bad}                                   # 여전히 섞임 / 나머지는 응답 없음
    (c,) = graph.grade_candidates("q", _two()[:1], lang)
    assert "name_en" not in c and "desc_en" not in c and c["reason"] is None
    assert c["name"] == "검정카드지갑" and c["grade"] == 85               # 점수·원문은 그대로
    assert store.metrics_since("2000-01-01").get("ai_lang_fail") == 3


def test_korean_report_does_not_translate(ai):
    ai["grade"] = [{"id": 0, "score": 70, "reason": "색 일치"}]
    out = graph.grade_candidates("검정 지갑", _two()[:1], "ko")
    assert out[0]["reason"] == "색 일치" and [c[0] for c in ai["calls"]] == ["grade"]


def test_translation_failure_keeps_grading(ai):
    ai["grade"] = [{"id": 0, "score": 70, "reason": GOOD["ja"][2]}]
    ai["items"] = RuntimeError("gemini down")
    (c,) = graph.grade_candidates("財布", _two()[:1], "ja")
    assert c["grade"] == 70 and c["reason"] == GOOD["ja"][2] and "name_en" not in c


def test_grade_gets_selected_or_extracted_lost_date(ai):
    ai["grade"] = []
    graph.n_grade({"text": "q", "candidates": _two(), "lang": "ko", "lost_date": "2026-11-10",
                   "extracted": {"lost_date": "2026-11-01"}})
    graph.n_grade({"text": "q", "candidates": _two(), "lang": "ko", "extracted": {"lost_date": "2026-11-01"}})
    assert [c[2] for c in ai["calls"] if c[0] == "grade"] == ["2026-11-10", "2026-11-01"]


# ── 메일 번역도 같은 검사 ──

def test_mail_translation_drops_mixed_values_but_keeps_place_originals(monkeypatch):
    replies = iter([
        {"name_0": "Black Card Wallet", "color_0": "黒", "place_held_0": "강남경찰서 (江南警察署)"},  # 1차: 이름이 영어
        {"name_0": "Black Card Wallet"},                                                              # 다시 써도 영어
    ])
    monkeypatch.setattr(gemini, "generate_json", lambda prompt, timeout=60: next(replies))
    out = gemini.translate_strings({"name_0": "검정카드지갑", "color_0": "검정", "place_held_0": "강남경찰서"}, "ja")
    assert out == {"color_0": "黒", "place_held_0": "강남경찰서 (江南警察署)"}   # 이름만 빠짐 → 메일은 원문
