"""결과 언어 검사 — AI가 만든 물품명·설명·판정 이유가 신고자 언어로 나왔는지(문자 체계 기준, 비용 0).

언어 감지 모델 없이 문자 범위로만 본다(짧은 물품명은 감지기가 흔들림). 원칙:
- 한글이 남으면 실패(한국어 신고 제외). 한국 브랜드도 로마자로 옮겨야 함(예: 신한 → Shinhan).
- 일본어: 가나·한자가 있어야. 중국어: 한자가 있고 가나가 없어야 + 간체/번체 전용 글자가 반대쪽에 섞이면 실패.
  태국어: 태국 문자. 베트남어: 베트남어 성조·모음 부호.
- 목표 문자가 하나도 없는데 영어 문장처럼 보이면(라틴 단어 3개 이상, 또는 흔한 영어 낱말) 실패.
  'iPhone 15 Pro'·'AirPods'처럼 브랜드·모델명만 있는 이름은 통과.
- 영어(그 밖의 라틴 문자 언어): 한글·가나·한자·태국 문자가 없으면 통과.
"""

from __future__ import annotations

import re

_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")
_THAI = re.compile(r"[฀-๿]")
_VI = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
# 습득물 결과에 자주 나오는 영어 낱말 — 일본어·중국어·태국어·베트남어 칸에 이게 보이면 영어로 나온 것
_EN_COMMON = {
    "the", "and", "with", "for", "of", "in", "on", "at", "is", "are", "was", "this", "that", "it", "its", "not", "no",
    "wallet", "card", "cards", "bag", "phone", "case", "cash", "key", "keys", "glasses", "umbrella", "leather", "black",
    "white", "brown", "blue", "red", "gray", "grey", "green", "pink", "beige", "navy", "silver", "gold", "found",
    "lost", "item", "items", "color", "brand", "matches", "match", "similar", "same", "different", "holder", "purse",
    "backpack", "earphones", "earbuds", "charger", "laptop", "watch", "ring", "hat", "cap", "shopping", "wireless",
    "men", "women", "mens", "womens", "small", "large", "credit", "check", "transit", "inside", "contains", "possible",
}
# 간체·번체 구분용(이 서비스 결과에 자주 나오는 글자만 — 완전한 판별은 아님)
_SIMP_ONLY = set("钱机车码电脑镜伞钥证红蓝绿带这个为与们说时对应颜发现无线银铁书门长东里买卖饰钟表凭务")
_TRAD_ONLY = set("錢機車碼電腦鏡傘鑰證紅藍綠帶這個為與們說時對應顏發現無線銀鐵書門長東裡買賣飾鐘錶憑務")


def _looks_english(text: str) -> bool:
    words = [w.lower() for w in _LATIN_WORD.findall(text)]
    return len(words) >= 3 or any(w in _EN_COMMON for w in words)


def lang_ok(text: str | None, lang: str) -> bool:
    """text가 lang(정리된 코드: en·ja·zh-CN·zh-TW·vi·th 등)으로 쓰였다고 볼 수 있는가. 빈 값은 통과(검사할 게 없음)."""
    t = (text or "").strip()
    if not t or lang == "ko":
        return True
    if _HANGUL.search(t):
        return False
    base = lang.split("-")[0]
    if base == "ja":
        return bool(_KANA.search(t) or _HAN.search(t)) or not _looks_english(t)
    if base == "zh":
        if _KANA.search(t):
            return False
        if lang == "zh-TW" and any(ch in _SIMP_ONLY for ch in t):
            return False
        if lang == "zh-CN" and any(ch in _TRAD_ONLY for ch in t):
            return False
        return bool(_HAN.search(t)) or not _looks_english(t)
    if base == "th":
        return bool(_THAI.search(t)) or not _looks_english(t)
    if base == "vi":
        words = re.findall(r"[^\W\d_]+", t)
        marked = sum(1 for w in words if _VI.search(w))
        if len(words) >= 4 and marked / len(words) < 0.3:
            return False  # 성조 부호를 뺀 베트남어("La vi the mau den duoc nhat…") — 11/15 측정에서 나옴
        return bool(marked) or not _looks_english(t)
    # 영어와 그 밖의 라틴 문자 언어: 다른 문자 체계가 섞이면 실패
    return not (_KANA.search(t) or _HAN.search(t) or _THAI.search(t))
