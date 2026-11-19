"""지역 이름 영문 표기 — 영어 화면(그리고 그 위에서 도는 구글 번역)에서 '서울특별시 중구' 대신 'Jung-gu, Seoul'.

- 시도 17곳은 공식 영문 이름(표). 시군구는 국어의 로마자 표기법(문화체육관광부 고시)대로 옮긴다:
  - 소리 나는 대로: 종로 → Jongno, 강릉 → Gangneung, 동래 → Dongnae, 울릉 → Ulleung, 철원 → Cheorwon, 덕양 → Deogyang
    (받침 + ㅇ 이어 읽기, ㄱ·ㄷ·ㅂ + ㄴ·ㅁ 콧소리, ㅁ·ㅇ + ㄹ → ㄴ, ㄴ·ㄹ + ㄹ·ㄴ → ll)
  - 행정 단위(시·군·구·도·읍·면·동) 앞엔 붙임표, 붙임표 앞뒤 소리 변화는 반영하지 않음: 종로구 → Jongno-gu
  - 된소리·'ㅎ' 뒤 거센소리는 체언이라 적지 않음(묵호 → Mukho)
- 화면 순서는 작은 단위부터 쉼표로(영문 주소 관례): '용인시 처인구' → 'Cheoin-gu, Yongin-si'.
AI 없이 규칙만 — 같은 이름은 늘 같은 표기, 비용 0. 표에 없는 새 시도는 규칙대로 옮김.
"""

from __future__ import annotations

SIDO_EN = {
    "서울특별시": "Seoul", "부산광역시": "Busan", "대구광역시": "Daegu", "인천광역시": "Incheon",
    "광주광역시": "Gwangju", "대전광역시": "Daejeon", "울산광역시": "Ulsan", "세종특별자치시": "Sejong",
    "경기도": "Gyeonggi-do", "강원특별자치도": "Gangwon State", "강원도": "Gangwon-do",
    "충청북도": "Chungcheongbuk-do", "충청남도": "Chungcheongnam-do",
    "전북특별자치도": "Jeonbuk State", "전라북도": "Jeollabuk-do", "전라남도": "Jeollanam-do",
    "경상북도": "Gyeongsangbuk-do", "경상남도": "Gyeongsangnam-do",
    "제주특별자치도": "Jeju-do", "제주도": "Jeju-do",
}

_INI = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_VOW = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi",
        "yu", "eu", "ui", "i"]
# 받침 번호 → 대표 자음(초성 번호, 이어 읽기에 씀). 겹받침은 앞 자음으로(지명엔 거의 없음).
_FIN_AS_INI = [None, 0, 1, 0, 2, 2, 2, 3, 5, 0, 6, 7, 5, 5, 7, 5, 6, 7, 7, 9, 10, 11, 12, 14, 15, 16, 17, 18]
# 받침 소리(단어 끝·자음 앞): ㄱ류 k, ㄴ n, ㄷ·ㅅ·ㅈ·ㅊ·ㅌ·ㅎ류 t, ㄹ l, ㅁ m, ㅂ·ㅍ p, ㅇ ng
_FIN = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l", "l", "p", "l", "m", "p", "p", "t", "t",
        "ng", "t", "t", "k", "t", "p", "t"]
_K_GROUP, _T_GROUP, _P_GROUP = {1, 2, 3, 9, 24}, {7, 19, 20, 22, 23, 25, 27}, {17, 18, 26}
_NI, _MI, _RI, _OI = 2, 6, 5, 11          # 초성 ㄴ·ㅁ·ㄹ·ㅇ
_N, _L, _M, _NG = 4, 8, 16, 21            # 받침 ㄴ·ㄹ·ㅁ·ㅇ
_UNITS = {"시": "si", "군": "gun", "구": "gu", "도": "do", "읍": "eup", "면": "myeon", "동": "dong"}


def _jamo(word: str) -> list[tuple[int, int, int] | str]:
    out: list[tuple[int, int, int] | str] = []
    for ch in word:
        c = ord(ch) - 0xAC00
        out.append((c // 588, (c % 588) // 28, c % 28) if 0 <= c < 11172 else ch)
    return out


def romanize(word: str) -> str:
    """한 낱말(붙임표 없는 덩어리)을 로마자로 — 받침과 다음 첫소리 사이 소리 변화를 반영, 소문자."""
    syl = _jamo(word)
    heads = [_INI[s[0]] if isinstance(s, tuple) else "" for s in syl]
    tails = []
    for i, s in enumerate(syl):
        if not isinstance(s, tuple):
            tails.append("")
            continue
        f = s[2]
        nxt = syl[i + 1][0] if i + 1 < len(syl) and isinstance(syl[i + 1], tuple) else None
        tail = _FIN[f]
        if f and nxt is not None:
            if nxt == _OI and f != _NG:                   # 이어 읽기: 철원 → Cheorwon, 덕양 → Deogyang (ㅎ은 소리 없음)
                if f != 27:
                    heads[i + 1] = _INI[_FIN_AS_INI[f]]
                tail = ""
            elif nxt in (_NI, _MI):                       # 콧소리: 백마 → Baengma / ㄹ+ㄴ → ll(설날 → Seollal)
                if f == _L and nxt == _NI:
                    tail, heads[i + 1] = "l", "l"
                elif f in _K_GROUP:
                    tail = "ng"
                elif f in _T_GROUP:
                    tail = "n"
                elif f in _P_GROUP:
                    tail = "m"
            elif nxt == _RI:
                if f in (_N, _L):                         # 신라 → Silla, 울릉 → Ulleung
                    tail, heads[i + 1] = "l", "l"
                elif f in (_M, _NG):                      # 종로 → Jongno, 강릉 → Gangneung
                    heads[i + 1] = "n"
                elif f in _K_GROUP:                       # 백령 → Baengnyeong
                    tail, heads[i + 1] = "ng", "n"
                elif f in _P_GROUP:
                    tail, heads[i + 1] = "m", "n"
                elif f in _T_GROUP:
                    tail, heads[i + 1] = "n", "n"
        tails.append(tail)
    return "".join(heads[i] + _VOW[s[1]] + tails[i] if isinstance(s, tuple) else s for i, s in enumerate(syl))


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _token(tok: str) -> str:
    """'처인구' → 'Cheoin-gu' (행정 단위 앞 붙임표), 그 밖은 통째로."""
    if len(tok) > 1 and tok[-1] in _UNITS:
        return _cap(romanize(tok[:-1])) + "-" + _UNITS[tok[-1]]
    return _cap(romanize(tok))


def sigungu_en(label: str) -> str:
    """시도 아래 이름 → 영문(작은 단위부터): '용인시 처인구' → 'Cheoin-gu, Yongin-si'."""
    return ", ".join(_token(t) for t in reversed(label.split()))


def sido_en(sido: str) -> str:
    return SIDO_EN.get(sido) or _token(sido)


def region_en(region: str) -> str:
    """'강원특별자치도 삼척시' → 'Samcheok-si, Gangwon State'. 시도만이면 시도 영문, 빈값은 빈값."""
    parts = (region or "").split()
    if not parts:
        return ""
    if parts[0] in SIDO_EN:
        rest = " ".join(parts[1:])
        return f"{sigungu_en(rest)}, {SIDO_EN[parts[0]]}" if rest else SIDO_EN[parts[0]]
    return sigungu_en(" ".join(parts))
