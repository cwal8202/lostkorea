"""습득물 특이사항(uniq) 정리 — 경찰 상투 문장을 걷어내고 단서만. 메일·판정·이탈 기록이 같은 기준을 쓴다."""

from __future__ import annotations

import re

_NOTE_RE = re.compile(r"특이\s*사항\s*[:：]?\s*(.*)$", re.S)
_NOTE_NONE = re.compile(r"^(없음|없습니다|해당\s*없음|무|-|\.)?$")
_BOILER = (re.compile(r"^.*?습득\s*/?\s*보관\s*하였습니다\.?", re.S), re.compile(r"분실하신\s*분께서는.*?바랍니다\.?", re.S))


def clean_desc(s) -> str:
    """특이사항(uniq)에서 단서만 남김 — '내용' 라벨·줄바꿈 정리.

    경찰 원문은 "○○에서는 [날짜] [물건]을 습득/보관 하였습니다 … 특이사항 : X" 틀이라 앞 140자를 자르면
    상투 문장만 남았음 → 끝의 X만(없음이면 빈칸). 틀이 아닌 글은 상투 문장만 걷어내고 나머지를 씀.
    """
    s = re.sub(r"^\s*내용\s*", "", str(s or ""))
    m = _NOTE_RE.search(s)
    if m:
        v = re.sub(r"\s+", " ", m.group(1)).strip()
        return "" if _NOTE_NONE.match(v) else v
    for rx in _BOILER:
        s = rx.sub("", s)
    return re.sub(r"\s+", " ", s).strip()
