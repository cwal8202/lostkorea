"""실제 분실 신고 실측(eval/run_lost112.py) — 신고 칸 → 검색 문장 만들기."""

from eval.run_lost112 import _eul, to_text


def test_eul_follows_final_consonant():
    assert _eul("지갑") == "을"
    assert _eul("파우치") == "를"
    assert _eul("에어팟(프로)") == "를"      # 괄호 뒤가 아니라 괄호 앞 글자로
    assert _eul("iPhone") == "을(를)"       # 한글이 아니면 둘 다


def test_to_text_uses_owner_fields():
    r = {"lstYmd": "2026-11-12", "lstHor": "09", "lstLctNm": "부산광역시", "lstPlace": "부산 131번 버스",
         "lstPlaceSeNm": "버스", "clrNm": "갈색(갈)", "lstPrdtNm": "곰 캐릭터 파우치형 지갑",
         "lstSbjt": "131번 버스 이용 중 갈색 곰 캐릭터 지갑 분실", "uniq": ""}
    t = to_text(r)
    assert t.startswith("2026-11-12 9시쯤 부산광역시 부산 131번 버스에서 갈색 곰 캐릭터 파우치형 지갑을 잃어버렸어요.")
    assert "131번 버스 이용 중" in t


def test_to_text_skips_color_already_in_name_and_unknown_hour():
    r = {"lstYmd": "2026-10-24", "lstHor": "", "lstLctNm": "서울특별시", "lstPlace": "CU 종로3가점",
         "lstPlaceSeNm": "불상", "clrNm": "블랙(검정)", "lstPrdtNm": "블랙 입생로랑 지갑", "lstSbjt": "지갑 분실"}
    t = to_text(r)
    assert "블랙 블랙" not in t
    assert "시쯤" not in t
    assert "(불상)" in t
