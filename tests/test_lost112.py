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


def _closure(item_id, region, found_at, sub="여성용 지갑", color="블랙(검정)", top="지갑"):
    return {"item_id": item_id, "category_top": top, "category_sub": sub, "color": color, "region": region,
            "found_at": found_at}


def test_closed_pool_keeps_same_kind_place_and_time_only():
    """거꾸로 보기의 종결 풀 — 같은 대분류·분실일 하루 전~30일 뒤·지역이 이어지는 것만, 경유 지역이 앞."""
    from eval.run_lost112 import _pool

    r = {"prdtClNm": "지갑 > 여성용 지갑", "lstLctNm": "서울특별시", "clrNm": "블랙(검정)", "lstYmd": "2026-11-10"}
    cl = [_closure("A_1", "서울특별시 송파구", "2026-11-11"),
          _closure("B_1", "서울특별시 마포구", "2026-11-12"),
          _closure("C_1", "부산광역시 남구", "2026-11-11"),             # 다른 시도
          _closure("D_1", "서울특별시 송파구", "2026-11-05"),           # 분실일보다 닷새 앞
          _closure("E_1", "서울특별시 송파구", "2026-11-11", top="가방"),  # 다른 대분류
          _closure("F_1", "서울특별시 송파구", "2026-11-11")]
    assert [c["item_id"] for c in _pool(r, ["서울 송파"], cl, {"F_1"}, 10)] == ["A_1", "B_1"]
    assert len(_pool(r, ["서울 송파"], cl, set(), 1)) == 1


def test_closed_pool_maps_old_and_merged_province_names():
    """신고는 '강원도'·'전남광주통합특별시'처럼 쓰고 습득물은 '강원특별자치도'·'광주광역시'로 씀 — 이어지게."""
    from eval.run_lost112 import _pool

    for sido, region in (("강원도", "강원특별자치도 태백시"), ("전남광주통합특별시", "광주광역시 광산구"),
                         ("전남광주통합특별시", "전라남도 순천시")):
        r = {"prdtClNm": "지갑 > 여성용 지갑", "lstLctNm": sido, "clrNm": "", "lstYmd": "2026-11-10"}
        assert _pool(r, [], [_closure("A_1", region, "2026-11-10")], set(), 5), (sido, region)
