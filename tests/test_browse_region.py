"""전체 습득물 — 지역 고르기를 입력하면 걸러지는 목록(자동완성)으로. 화면 틀(한·영)과 로마자 변환 표를 확인."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app, follow_redirects=False)
APP_JS = (Path(__file__).resolve().parent.parent / "apps" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_browse_region_is_a_typeable_combobox_in_both_languages():
    for path, ph in (("/browse", "지역 입력 또는 선택"), ("/en/browse", "Type or pick a region")):
        html = client.get(path).text
        assert 'id="bregionInput"' in html and 'role="combobox"' in html and 'aria-controls="bregionList"' in html
        assert f'placeholder="{ph}"' in html
        assert 'id="bregion"' in html and '<select id="bregion"' not in html  # 값은 숨은 칸(주소창 ?region= 그대로)


def _table(name: str) -> list[str]:
    return json.loads(re.search(rf"const {name} = (\[[^\]]*\]);", APP_JS).group(1))


def _romanize(s: str) -> str:  # app.js romanize()와 같은 계산 — 표가 어긋나면 외국인 영어 입력이 엉뚱한 곳을 찾음
    ini, vow, fin = _table("RR_I"), _table("RR_V"), _table("RR_F")
    assert (len(ini), len(vow), len(fin)) == (19, 21, 28)
    out = ""
    for ch in s:
        c = ord(ch) - 0xAC00
        out += ch if c < 0 or c > 11171 else ini[c // 588] + vow[(c % 588) // 28] + fin[c % 28]
    return out


def test_romanized_region_names_match_what_foreigners_type():
    for ko, en in (("부산", "busan"), ("서울", "seoul"), ("강남구", "gangnamgu"), ("해운대구", "haeundaegu"),
                   ("광주", "gwangju"), ("제주", "jeju"), ("인천", "incheon"), ("경기도", "gyeonggido")):
        assert _romanize(ko) == en
