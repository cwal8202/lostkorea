"""지역 이름 영문 표기(영어 화면) — 공식 표기와 같은지, 소리 변화·붙임표 규칙."""

from apps.api.region_en import region_en, romanize, sido_en, sigungu_en
from apps.api.search import region_tree


def test_sound_changes_follow_official_spelling():
    # 소리 나는 대로(콧소리·ㄹ·이어 읽기) — 글자대로 옮기면 Jongro·Gangreung·Ulreung·Cheolwon이 됨
    cases = {"종로": "jongno", "강릉": "gangneung", "동래": "dongnae", "울릉": "ulleung", "중랑": "jungnang",
             "상록": "sangnok", "철원": "cheorwon", "밀양": "miryang", "덕양": "deogyang", "신라": "silla",
             "백령": "baengnyeong", "익산": "iksan", "합천": "hapcheon", "의령": "uiryeong", "정읍": "jeongeup"}
    for ko, en in cases.items():
        assert romanize(ko) == en, ko


def test_units_get_hyphen_and_small_unit_first():
    assert sigungu_en("종로구") == "Jongno-gu"                    # 붙임표 앞뒤 소리 변화 없음(Jongnogu 아님)
    assert sigungu_en("용인시 처인구") == "Cheoin-gu, Yongin-si"
    assert sigungu_en("고양시 일산서구") == "Ilsanseo-gu, Goyang-si"
    assert sido_en("강원특별자치도") == "Gangwon State" and sido_en("서울특별시") == "Seoul"


def test_region_en_full_and_tree():
    assert region_en("강원특별자치도 삼척시") == "Samcheok-si, Gangwon State"
    assert region_en("서울특별시") == "Seoul" and region_en("") == ""
    tree = region_tree([{"key": "서울특별시 중구", "doc_count": 3}, {"key": "서울특별시", "doc_count": 1}])
    assert tree[0] == {"name": "서울특별시", "count": 4, "level": 0, "en": "Seoul"}
    assert tree[1]["en"] == "Jung-gu" and tree[1]["label"] == "중구"
