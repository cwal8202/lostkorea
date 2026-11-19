"""습득물의 정해진 낱말 → 영어(영어 화면용) — 분류·색·습득 장소·경찰 보관 기관. AI 없이 표와 규칙.

경찰청 자료는 이 칸들이 정해진 값 중 하나라(2026-11-20 운영 색인: 분류 21·세부 약 70, 색 49, 습득 장소 19) 한 번 옮겨 두면
늘 같은 표기·비용 0. 표에 없는 새 값은 한국어 그대로(지어내지 않음).
경찰 보관 기관(경찰서·지구대·파출소·치안센터 — 보관 기관의 약 3분의 2)은 지명 로마자 + 기관 종류 규칙:
'서울남대문경찰서' → 'Seoul Namdaemun Police Station', '종로2가지구대' → 'Jongno 2-ga Police Substation'.
역·공항·쇼핑몰 같은 나머지 기관 이름은 수집 때 AI로 한 번 옮겨 색인에 저장(apps/collector/translate_en.py).
다른 12개 언어는 이 영어 화면(또는 한국어 화면) 위의 구글 번역이 맡음.
"""

from __future__ import annotations

import re

from .region_en import _cap, romanize

CATEGORY_TOP_EN = {
    "기타물품": "Other items", "지갑": "Wallet", "카드": "Card", "전자기기": "Electronics", "의류": "Clothing",
    "현금": "Cash", "가방": "Bag", "쇼핑백": "Shopping bag", "휴대폰": "Phone", "증명서": "ID & certificates",
    "귀금속": "Jewelry", "자동차": "Car items", "스포츠용품": "Sports gear", "유가증권": "Vouchers & securities",
    "도서용품": "Books", "서류": "Documents", "무주물": "Ownerless item", "컴퓨터": "Computer",
    "산업용품": "Industrial goods", "유류품": "Left-behind belongings", "악기": "Musical instrument",
}
CATEGORY_SUB_EN = {
    "기타": "Other", "안경": "Glasses", "선글라스": "Sunglasses", "매장문화재": "Buried cultural property",
    "기타 지갑": "Other wallet", "남성용 지갑": "Men's wallet", "여성용 지갑": "Women's wallet",
    "신용(체크)카드": "Credit/debit card", "교통카드": "Transit card", "기타카드": "Other card", "일반카드": "Card",
    "현금": "Cash", "외화": "Foreign currency", "수표": "Check",
    "무선이어폰": "Wireless earbuds", "기타용품": "Other accessories", "스마트워치": "Smartwatch", "태블릿": "Tablet",
    "카메라": "Camera", "스포츠용품": "Sports gear",
    "모자": "Hat", "기타의류": "Other clothing", "여성의류": "Women's clothing", "남성의류": "Men's clothing",
    "신발": "Shoes", "아기의류": "Baby clothing",
    "기타가방": "Other bag", "여성용가방": "Women's bag", "남성용가방": "Men's bag", "쇼핑백": "Shopping bag",
    "삼성휴대폰": "Samsung phone", "아이폰": "iPhone", "기타휴대폰": "Other phone", "기타통신기기": "Other mobile device",
    "LG휴대폰": "LG phone",
    "신분증": "ID card", "면허증": "Driver's license", "여권": "Passport",
    "반지": "Ring", "시계": "Watch", "목걸이": "Necklace", "귀걸이": "Earrings",
    "자동차열쇠": "Car key", "자동차번호판": "License plate", "임시번호판": "Temporary plate",
    "상품권": "Gift voucher", "어음": "Promissory note",
    "기타서적": "Other books", "소설": "Novel", "학습서적": "Study book", "만화책": "Comic book",
    "컴퓨터서적": "Computer book", "서류": "Documents", "기타물품": "Other items", "무주물": "Ownerless item",
    "삼성노트북": "Samsung laptop", "LG노트북": "LG laptop", "애플노트북": "Apple laptop",
    "유류품": "Left-behind belongings", "무안공항유류품": "Muan Airport belongings",
    "기타악기": "Other instrument", "현악기": "String instrument", "관악기": "Wind instrument",
    "타악기": "Percussion", "건반악기": "Keyboard instrument",
}
COLOR_EN = {
    "화이트(흰)": "White", "블랙(검정)": "Black", "그레이(회)": "Gray", "기타": "Other", "브라운(갈)": "Brown",
    "블루(파랑)": "Blue", "코발트(짙은청록)": "Cobalt (dark teal)", "엘로우(노랑)": "Yellow",
    "딥스카이블루(새파란하늘)": "Deep sky blue", "코럴(산호)": "Coral", "피치오렌지(오렌지)": "Peach orange",
    "엘로우(밝은노랑)": "Light yellow", "그린(초록)": "Green", "핑크(연한핑크)": "Light pink", "레드(빨강)": "Red",
    "로열블루(밝은남)": "Royal blue", "레드(연한레드)": "Light red", "시안(청록)": "Cyan",
    "다크오렌지(어두운오렌지)": "Dark orange", "실버(은)": "Silver", "퓨시아(자홍)": "Fuchsia", "바이올렛(보라)": "Violet",
    "오렌지(선명한오렌지)": "Bright orange", "블루바이올렛(남보라)": "Blue violet", "라임그린(어두운청록)": "Lime green",
    "다크레드(진빨강)": "Dark red", "오렌지(연한오렌지)": "Light orange", "샤르트뢰즈(역녹)": "Chartreuse",
    "알리자린(홍)": "Crimson", "에메랄드(선녹)": "Emerald", "라이트그레이(옅은회)": "Light gray",
    "그레이(암회)": "Dark gray", "씨그린(노르스름한녹)": "Sea green", "올리브(녹갈)": "Olive",
    "다크시안(어두운청록)": "Dark cyan", "네온그린(형광초록)": "Neon green", "터키오이스(옥)": "Turquoise",
    "라임(라임)": "Lime", "레드(연한빨강)": "Light red", "코발트(밝은남)": "Cobalt blue", "핑크(분홍)": "Pink",
    "다크블루(어두운파랑)": "Dark blue", "다크핑크(진분홍)": "Dark pink", "핑크(선명한분홍)": "Bright pink",
    "스트롱시안(진한청록)": "Strong cyan", "다크바이올렛(진보라)": "Dark violet", "마젠타(자홍)": "Magenta",
    "블루(어두운파랑)": "Dark blue", "오렌지(주황)": "Orange",
}
FOUND_PLACE_EN = {
    "기차": "Train", "지하철": "Subway", "노상": "Street", "우체국(통)": "Post office / mailbox",
    "백화점/매장": "Department store / shop", "공항": "Airport", "기타": "Other", "놀이공원": "Amusement park",
    "버스": "Bus", "영화관": "Cinema", "택시": "Taxi", "유원지": "Park / resort", "공공기관": "Public office",
    "상점": "Store", "회사": "Office", "음식점(업소포함)": "Restaurant / bar", "학교": "School", "불상": "Unknown",
    "주택": "House",
}

_POLICE = (("치안센터", "Police Center"), ("경찰서", "Police Station"), ("지구대", "Police Substation"),
           ("파출소", "Police Box"))
# 경찰서 이름 앞머리 지명(떼어 쓰기): 광역 → 시. 남은 글자가 2자 이상일 때만('부산진' → Busanjin, '부산 진' 아님)
_CITY = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남",
         "경북", "경남", "제주")
_CITY2 = ("남양주", "의정부", "수원", "성남", "안양", "안산", "고양", "용인", "청주", "천안", "전주", "포항", "창원",
          "화성", "부천", "김해", "평택", "일산", "분당")
_DIR = ("동부", "서부", "남부", "북부", "중부")


def _words(base: str) -> str:
    words: list[str] = []
    for c in _CITY + _CITY2:
        if base.startswith(c) and len(base) - len(c) >= 2:
            words.append(c)
            base = base[len(c):]
            break
    tail = next((d for d in _DIR if base.endswith(d) and len(base) > len(d)), "")
    if tail:
        base = base[: -len(tail)]
    m = re.match(r"^([가-힣]+?)(\d+)(가)?$", base)
    words += [m.group(1), m.group(2) + ("-ga" if m.group(3) else "")] if m else [base]
    if tail:
        words.append(tail)
    return " ".join(_cap(romanize(w)) if re.search(r"[가-힣]", w) else w for w in words if w)


def police_en(name: str) -> str:
    """경찰 보관 기관 이름 → 영어. 경찰 기관이 아니면 ''(호출부가 AI 번역·원문)."""
    name = (name or "").strip()
    if not re.fullmatch(r"[가-힣0-9]+", name):
        return ""
    for ko, en in _POLICE:
        if name.endswith(ko) and len(name) > len(ko):
            return f"{_words(name[: -len(ko)])} {en}"
    return ""


def category_en(top: str, sub: str, full: str = "") -> str:
    """'카드' + '신용(체크)카드' → 'Card > Credit/debit card'. 모르는 값이 있으면 ''(한국어 그대로 보임)."""
    if not top and full:
        top, _, sub = (p.strip() for p in full.partition(">"))
    t = CATEGORY_TOP_EN.get(top or "")
    if not t:
        return ""
    if not sub:
        return t
    s = CATEGORY_SUB_EN.get(sub)
    return f"{t} > {s}" if s else ""


def color_en(color: str) -> str:
    return COLOR_EN.get(color or "", "")


def found_place_en(place: str) -> str:
    return FOUND_PLACE_EN.get(place or "", "")
