"""습득물 영어 표시(FIND-124) — 정해진 낱말 표·경찰 기관 규칙, AI 번역 검사, 수집 때 번역(기억·색인)."""

from apps import store
from apps.api.search import _item
from apps.api.vocab_en import category_en, color_en, found_place_en, police_en
from apps.collector import translate_en as T


def test_police_names_follow_rules():
    assert police_en("서울남대문경찰서") == "Seoul Namdaemun Police Station"
    assert police_en("부산진경찰서") == "Busanjin Police Station"            # '부산 진'으로 쪼개지 않음
    assert police_en("남양주남부경찰서") == "Namyangju Nambu Police Station"
    assert police_en("종로2가지구대") == "Jongno 2-ga Police Substation"
    assert police_en("산호파출소") == "Sanho Police Box"
    assert police_en("신분당선 유실물센터") == "" and police_en("경찰서") == ""   # 경찰 기관 아님·이름 없음 → AI·원문


def test_fixed_vocab_tables():
    assert category_en("카드", "신용(체크)카드") == "Card > Credit/debit card"
    assert category_en("", "", "지갑 > 여성용 지갑") == "Wallet > Women's wallet"
    assert category_en("새분류", "뭔가") == ""                              # 모르는 값은 지어내지 않음(한국어 그대로)
    assert color_en("코발트(짙은청록)") == "Cobalt (dark teal)" and color_en("") == ""
    assert found_place_en("지하철") == "Subway"


def test_ai_translation_must_keep_brands_and_drop_korean():
    assert T.valid("DAKS 지갑", "DAKS wallet")
    assert T.valid("갤럭시s1 휴대폰", "Galaxy S1 phone")                   # 대소문자는 달라도 됨
    assert not T.valid("갤럭시s1 휴대폰", "Galaxy phone")                  # 모델 번호가 빠짐
    assert not T.valid("검정 지갑", "Black 지갑")                          # 한글이 남음
    assert not T.valid("검정 지갑", "")


def _doc(name, desc="", org="삼척경찰서", dep="산호파출소", found="2026-11-18"):
    return {"name": name, "description": desc, "org_name": org, "dep_place": dep, "found_at": found}


def test_sync_translates_new_strings_once_and_updates_index(monkeypatch):
    store.init()
    store.tr_put("name", {"현금": "Cash"})                                # 이미 기억에 있는 글은 AI에 안 보냄
    docs = {"a": _doc("현금"), "b": _doc("DAKS 지갑", "특이사항 : 카드 3장 들어있음", org="롯데월드어드벤처", dep=""),
            "c": _doc("검정 우산", found="2026-11-01")}
    monkeypatch.setattr("apps.collector.run._scan_docs", lambda q, f: docs)
    sent = []

    def fake(items):
        sent.extend(items)
        tr = {"DAKS 지갑": "DAKS wallet", "카드 3장 들어있음": "Contains 3 cards", "롯데월드어드벤처": "Lotte World Adventure",
              "검정 우산": "검정 umbrella"}                               # 한글이 남은 번역 → 버림
        return {kv: (tr[kv[1]] if T.valid(kv[1], tr[kv[1]]) else "") for kv in items}
    monkeypatch.setattr(T, "translate", fake)
    monkeypatch.setattr(T, "_ensure_mapping", lambda: None)
    updated = {}
    monkeypatch.setattr(T, "_bulk_update", lambda u: updated.update(u) or len(u))
    st = T.sync(max_docs=10, budget_s=60)
    assert ("name", "현금") not in sent and ("name", "DAKS 지갑") in sent and ("note", "카드 3장 들어있음") in sent
    assert ("place", "삼척경찰서") not in sent                              # 경찰 기관은 규칙(AI 안 씀)
    assert updated["a"] == {"name_mt": "Cash", "note_mt": "", "org_mt": "Samcheok Police Station", "dep_mt": "Sanho Police Box"}
    assert updated["b"]["name_mt"] == "DAKS wallet" and updated["b"]["note_mt"] == "Contains 3 cards"
    assert updated["b"]["org_mt"] == "Lotte World Adventure"
    assert updated["c"]["name_mt"] == ""                                   # 못 옮김 → 화면은 원문
    assert store.tr_get("name", ["검정 우산"]) == {"검정 우산": ""}         # 못 옮긴 것도 기억(매일 다시 부르지 않게)
    assert st["to_translate"] == 4 and st["rejected"] == 1


def test_sync_dry_run_writes_nothing(monkeypatch):
    store.init()
    monkeypatch.setattr("apps.collector.run._scan_docs", lambda q, f: {"x": _doc("새 물건 이름")})
    monkeypatch.setattr(T, "translate", lambda items: {kv: "New item" for kv in items})
    monkeypatch.setattr(T, "_bulk_update", lambda u: (_ for _ in ()).throw(AssertionError("쓰면 안 됨")))
    st = T.sync(max_docs=10, budget_s=60, dry=True)
    assert st["would_update"] == 1 and store.tr_get("name", ["새 물건 이름"]) == {}


def test_item_carries_english_fields():
    it = _item({"name": "지갑", "category_top": "지갑", "category_sub": "남성용 지갑", "color": "블랙(검정)",
                "found_place": "택시", "org_name": "서울강남경찰서", "dep_place": "역삼지구대", "name_mt": "Wallet",
                "region": "서울특별시 강남구"}, "F1_1")
    assert it["category_en"] == "Wallet > Men's wallet" and it["color_en"] == "Black" and it["found_place_en"] == "Taxi"
    assert it["org_mt"] == "Seoul Gangnam Police Station" and it["dep_mt"] == "Yeoksam Police Substation"
    assert it["name_mt"] == "Wallet" and it["region_en"] == "Gangnam-gu, Seoul"
