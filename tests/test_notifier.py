"""알림 헬퍼 — 특이사항 정리, 유효 이미지 필터(경찰청 no_img 플레이스홀더 제외)."""

from apps.notifier import _clean_desc, _html_body, _text_body, _valid_img


def test_weak_wording_differs_from_strong():
    lost, ms = {"text": "검정 지갑"}, [{"name": "지갑", "grade": 60}]
    assert "유력 후보" in _text_body(lost, ms)
    weak = _text_body(lost, ms, weak=True)
    assert "참고" in weak and "유력 후보" not in weak   # 약한 후보를 '유력'으로 과장하지 않음
    assert "참고" in _html_body(lost, ms, weak=True)


def test_clean_desc_strips_label_and_whitespace():
    assert _clean_desc("내용 카드  있음\n") == "카드 있음"
    assert _clean_desc("  줄바꿈\n\n정리  ") == "줄바꿈 정리"
    assert _clean_desc(None) == ""


def test_clean_desc_keeps_only_police_notes():
    """경찰 원문은 상투 문장 뒤 끝에 '특이사항'이 있음 → 메일엔 특이사항만(없음이면 빈칸)."""
    boiler = ("내용\r\n\r\n 난우파출소에서는 [2026.11.10]  [카드지갑]을 습득/보관 하였습니다.\r\n"
              "분실하신 분께서는 본인을 증명할 수 있는 서류를 지참하시어 보관중으로 기재되어 있는 기관에\r\n"
              "방문하시어 보관물품을 수령하시기 바랍니다.\r\n\r\n")
    assert _clean_desc(boiler + "특이사항 : 안에 신분증1매 있고 현금 없음") == "안에 신분증1매 있고 현금 없음"
    assert _clean_desc(boiler + "특이사항 : 없음") == ""
    assert _clean_desc(boiler) == ""                       # 특이사항 줄이 없는 틀도 상투 문장은 버림


def test_email_links_to_that_report_and_reads_naturally():
    from apps.notifier import build_email, report_link
    lost = {"id": "abc123", "text": "어제 홍대입구역 근처에서 검정 가죽 지갑을 잃어버렸어요", "lang": "ko"}
    link = report_link(lost, "tok_EN-1")
    assert link.endswith("#r=abc123.tok_EN-1")            # 토큰은 # 뒤(서버 기록에 안 남음)
    m = {"atc_id": "A_1", "name": "지갑", "color": "검정", "grade": 90, "reason": "색이 같아요"}
    _, text, html = build_email(lost, [m], link=link)
    assert link in text and f'href="{link}"' in html
    assert "과(와)" not in text and "'어제 홍대입구역 근처에서 검정 가죽 지갑을 잃어버렸어요' 신고와" in text
    assert "#r=" not in report_link({"lang": "en"}, "")      # id·토큰이 없으면 첫 화면 링크


def test_valid_img_filters_placeholder():
    assert _valid_img("http://x/real.jpg") == "http://x/real.jpg"
    assert _valid_img("https://minwon24.police.go.kr/images/sub/img02_no_img.gif") == ""
    assert _valid_img("") == ""
    assert _valid_img("ftp://x/img.jpg") == ""      # http(s) 아님
