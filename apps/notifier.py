"""알림(Notifier) — 채널 독립 인터페이스. 비즈니스 로직은 이 인터페이스로만 알림.

헌장: 코어에 카카오/웹 의존성 금지. v1은 ConsoleNotifier(로그), 추후 KakaoNotifier 등을 같은
인터페이스로 주입(코어 무변경). 되돌릴 수 없는 공식 신고 제출은 별도 HITL(여기선 통보만).
"""

from __future__ import annotations

import html as _html
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Protocol
from urllib.parse import quote as _quote

from apps.api import gemini
from apps.api.config import settings

TOP_OTHERS = 5  # 상위 매칭 외에 함께 보여줄 "비슷한 후보" 수
_LOGO = Path(__file__).resolve().parent / "web" / "static" / "logo-email.png"  # 메일 인라인 로고(CID, PNG)


def _emit(msg: str) -> None:
    """콘솔 인코딩(Windows cp949 등)이 이모지를 못 찍어도 요청을 죽이지 않도록 안전 출력."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write((msg + "\n").encode("utf-8", "replace"))
            buf.flush()


class Notifier(Protocol):
    def notify(self, lost: dict, matches: list[dict], weak: bool = False) -> None:
        """matches: grade 내림차순 후보 리스트(matches[0]=최고 매칭).
        weak=True: 임계 미만 '약한 후보' 참고 알림(신고자 옵트인 시에만) — 문구를 낮춰 기대치 조정."""
        ...

    def verify(self, to: str, lang: str, link: str) -> None:
        """이메일 확인(double opt-in) 메일 — 고정 문구 + 확인 링크만(사용자 입력 미포함 → 스팸 릴레이 불가)."""
        ...


def _top(matches: list[dict]) -> dict:
    return matches[0] if matches else {}


class ConsoleNotifier:
    """개발/데모용 — 콘솔로 매칭 알림 출력."""

    def notify(self, lost: dict, matches: list[dict], weak: bool = False) -> None:
        m = _top(matches)
        who = lost.get("user_id", "anon")
        text = (lost.get("text") or "")[:30]
        extra = max(len(matches) - 1, 0)
        head = "비슷한 후보(참고, 신뢰도 낮음)" if weak else "유력 후보 발견(확인 필요)!"
        _emit(
            f"🔔 [알림→{who}] 분실물 '{text}' {head}\n"
            f"    → {m.get('name')}/{m.get('color')} "
            f"(grade {m.get('grade')}) | 보관:{m.get('dep_place')} "
            f"지역:{m.get('region') or '-'} | 습득일:{m.get('found_at')}"
            + (f"\n    (외 비슷한 후보 {extra}건)" if extra else "")
        )

    def verify(self, to: str, lang: str, link: str) -> None:
        _emit(f"✉️ [확인 메일→{to}, {lang}] {link}")


_NOTE_RE = re.compile(r"특이\s*사항\s*[:：]?\s*(.*)$", re.S)
_NOTE_NONE = re.compile(r"^(없음|없습니다|해당\s*없음|무|-|\.)?$")
_BOILER = (re.compile(r"^.*?습득\s*/?\s*보관\s*하였습니다\.?", re.S), re.compile(r"분실하신\s*분께서는.*?바랍니다\.?", re.S))


def _clean_desc(s) -> str:
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


def _valid_img(url: str) -> str:
    """실제 이미지 URL만 통과(경찰청 no_img 플레이스홀더·빈값 제외)."""
    url = (url or "").strip()
    return url if url.startswith("http") and "no_img" not in url else ""


def _grade_color(g) -> str:
    if g is None:
        return "#9ca3af"
    return "#16a34a" if g >= 80 else "#d97706" if g >= 50 else "#9ca3af"


# 메일 고정 문구(한국어 원본). 외국어 사용자에겐 언어별로 한 번 번역해 캐시(_ui).
# {t}{n}{url}{lost112} 자리표시자는 번역 후에도 유지돼야 함(검증 실패 시 영어 → 한국어 폴백).
KO = {
    "tagline": "분실물 매칭 AI",
    "head_strong": "🔔 유력 후보를 찾았어요 — 확인해보세요",
    "desc_strong": "'{t}' 신고와 일치할 가능성이 높은 습득물을 찾았어요. "
                   "본인 물건이면 아래 보관기관으로 수령을 문의하세요.",
    "head_weak": "🔎 비슷한 후보가 있어요 — 참고용",
    "desc_weak": "'{t}' 신고와 정확히 일치하진 않지만(신뢰도 낮음) 비슷한 습득물이에요. "
                 "'약한 후보도 알림 받기'를 켜두셔서 보내드려요. 더 정확한 후보는 계속 찾고 있어요.",
    "subj_strong": "분실물 매칭 알림", "subj_weak": "비슷한 후보 알림(참고)",
    "item": "물품", "category": "분류", "held": "보관장소", "region": "지역", "found": "습득일",
    "desc": "특이사항", "office": "보관기관", "contact": "연락처", "ask_office": "보관기관 문의",
    "grade": "일치 점수", "score_badge": "일치", "reason": "판단 근거", "held_short": "보관",
    "others_text": "비슷한 후보 {n}건도 확인해보세요:", "others_html": "비슷한 후보 {n}건",
    "claim": "본인 물건이면 위 보관기관 연락처로 수령을 문의하세요.",
    "cta": "{brand}에서 전체 결과 보기 →", "cta_text": "{brand}에서 전체 결과 보기: {url}",
    "or_type": "버튼이 안 눌리면 이 링크를 여세요: {url}",
    "official": "공식 조회: {lost112} (경찰청 습득물 검색)",
    "footer": "최종 확인·수령은 본인이 진행하세요. 본 메일은 {brand} 자동 알림입니다.",
    "unsub": "이 신고의 알림 그만 받기",
}
# 경찰청 습득물 검색(경찰민원24). 옛 주소 lost112.go.kr은 11/12 기준 응답 없음 → 새 주소로.
POLICE_FOUND_SEARCH = "https://minwon24.police.go.kr/cvlcpt/cvlcptAply.do?cvlcptId=MW-201"
_PH = re.compile(r"\{(\w+)\}")
_UI_CACHE: dict[str, dict] = {}


def _fill(s: str, **kw) -> str:
    """자리표시자 치환(str.format 대신 — 번역문에 다른 중괄호가 있어도 안전)."""
    return _PH.sub(lambda m: str(kw.get(m.group(1), m.group(0))), s)


def _brand(table: dict) -> dict:
    """문구 속 {brand} → 서비스 이름(번역 때는 자리표시자로 두어 이름이 번역되지 않게)."""
    return {k: (v.replace("{brand}", settings.brand_name) if isinstance(v, str) else v) for k, v in table.items()}


def _table(name: str, base: dict, lang: str) -> dict:
    """고정 문구 표를 사용자 언어로. 실패하면 영어, 그것도 실패하면 한국어. 성공만 캐시(표·언어별)."""
    if lang == "ko":
        return _brand(base)
    key = f"{name}:{lang}"
    if key in _UI_CACHE:
        return _brand(_UI_CACHE[key])
    tr = gemini.translate_strings(base, lang)
    if tr and all(set(_PH.findall(tr[k])) >= set(_PH.findall(v)) for k, v in base.items()):
        _UI_CACHE[key] = tr
        return _brand(tr)
    return _table(name, base, "en") if lang != "en" else _brand(base)


def _ui(lang: str) -> dict:
    return _table("alert", KO, lang)


# 이메일 확인 메일 — 고정 문구만(사용자 입력을 싣지 않음: 남의 주소로 내용을 보내는 통로가 되지 않게)
KO_VERIFY = {
    "subject": "이메일 확인 — 분실물 알림 받기",
    "head": "알림 받을 이메일을 확인해주세요",
    "desc": "{brand}에서 이 주소로 분실물 알림을 받겠다는 요청이 있었어요. 본인이 요청했다면 아래 버튼을 눌러주세요. "
            "요청하지 않았다면 이 메일은 무시하면 돼요 — 확인하지 않으면 알림이 가지 않아요.",
    "button": "알림 받기 확인",
    "or_link": "버튼이 동작하지 않으면 이 주소를 열어주세요: {url}",
    "expires": "이 링크는 {h}시간 동안 유효해요.",
}


def build_verify_email(lang: str, link: str) -> tuple[str, str, str]:
    """(제목, 텍스트, HTML) — 확인 메일. 신고자 언어로 번역(고정 문구라 언어별 1회만)."""
    S = _table("verify", KO_VERIFY, gemini.normalize_lang(lang))
    e = _html.escape
    h = settings.verify_hours
    text = "\n".join([S["head"], "", S["desc"], "", link, "", _fill(S["expires"], h=h)])
    html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Malgun Gothic\',sans-serif;'
        'max-width:520px;color:#1f2430">'
        '<div style="font-size:20px;font-weight:800;color:#2563eb;padding-bottom:12px;'
        f'border-bottom:1px solid #eee;margin-bottom:16px">{e(settings.brand_name)}</div>'
        f'<h2 style="font-size:18px;margin:0 0 8px">{e(S["head"])}</h2>'
        f'<p style="margin:0 0 18px">{e(S["desc"])}</p>'
        f'<div style="text-align:center;margin:20px 0"><a href="{e(link)}" style="display:inline-block;'
        'background:#2563eb;color:#fff;text-decoration:none;padding:13px 30px;border-radius:10px;'
        f'font-weight:700">{e(S["button"])}</a></div>'
        f'<p style="font-size:12px;color:#6b7280">{_fill(e(S["or_link"]), url=e(link))}<br>'
        f'{e(_fill(S["expires"], h=h))}</p></div>'
    )
    return f"[{settings.brand_name}] {S['subject']}", text, html


def _localize(matches: list[dict], lang: str) -> list[dict]:
    """메일에 실을 후보들의 물품·장소 정보를 사용자 언어로(한 번의 번역 호출). 실패 시 원문 유지.

    물품명·설명은 판정 단계가 이미 사용자 언어로 만든 name_en/desc_en이 있으면 그걸 씀.
    장소·기관명은 '한국어 원문 (번역)'으로 — 보관기관에 그대로 보여줄 수 있게.
    """
    top = matches[: 1 + TOP_OTHERS]
    if lang == "ko":
        return top
    payload: dict[str, str] = {}
    for i, m in enumerate(top):
        if not m.get("name_en"):
            payload[f"name_{i}"] = m.get("name") or ""
        payload[f"color_{i}"] = m.get("color") or ""
        payload[f"cat_{i}"] = m.get("category") or ""
        payload[f"place_held_{i}"] = m.get("dep_place") or ""
        payload[f"place_region_{i}"] = m.get("region") or ""
        payload[f"place_org_{i}"] = m.get("org_name") or ""
    if top and not top[0].get("desc_en"):
        payload["desc_0"] = _clean_desc(top[0].get("description"))[:140]
    payload = {k: v for k, v in payload.items() if v}
    tr = gemini.translate_strings(payload, lang) or {}
    out = []
    for i, m in enumerate(top):
        mm = dict(m)
        mm["name"] = m.get("name_en") or tr.get(f"name_{i}", m.get("name"))
        mm["color"] = tr.get(f"color_{i}", m.get("color"))
        mm["category"] = tr.get(f"cat_{i}", m.get("category"))
        mm["dep_place"] = tr.get(f"place_held_{i}", m.get("dep_place"))
        mm["region"] = tr.get(f"place_region_{i}", m.get("region"))
        mm["org_name"] = tr.get(f"place_org_{i}", m.get("org_name"))
        if i == 0:
            mm["description"] = m.get("desc_en") or tr.get("desc_0", m.get("description"))
        out.append(mm)
    return out


def _intro(lost: dict, weak: bool, S: dict = KO) -> tuple[str, str]:
    """(제목줄, 설명) — 유력 후보 vs 약한 후보(참고)."""
    text = (lost.get("text") or "").strip()
    t = text[:40] + ("…" if len(text) > 40 else "")
    kind = "weak" if weak else "strong"
    return S[f"head_{kind}"], _fill(S[f"desc_{kind}"], t=t)


def _site(lost: dict) -> str:
    """메일 속 사이트 링크 — 한국어 외 신고자는 영어 화면(/en)으로."""
    base = settings.public_base_url.rstrip("/")
    return base if (lost.get("lang") or "ko") == "ko" else f"{base}/en"


def report_link(lost: dict, token: str) -> str:
    """그 신고의 결과로 바로 가는 링크. 토큰은 # 뒤(브라우저만 읽음 — 서버·프록시 기록에 남지 않음)."""
    site = _site(lost)
    page = site if site.endswith("/en") else site + "/"  # https://lostkorea.com/#r=… · https://lostkorea.com/en#r=…
    return f"{page}#r={lost.get('id')}.{token}" if lost.get("id") and token else site


def _text_body(lost: dict, matches: list[dict], weak: bool = False, S: dict = KO, link: str = "",
               unsub: str = "") -> str:
    m = _top(matches)
    head, desc = _intro(lost, weak, S)
    lines = [
        head,
        desc,
        "",
        f"• {S['item']}: {m.get('name')} / {m.get('color')}",
        f"• {S['category']}: {m.get('category')}",
        f"• {S['held']}: {m.get('dep_place') or '-'}",
        f"• {S['region']}: {m.get('region') or '-'}",
        f"• {S['found']}: {m.get('found_at') or '-'}",
        f"• {S['desc']}: {_clean_desc(m.get('description'))[:140] or '-'}",
        f"• {S['office']}: {m.get('org_name') or '-'}  ({S['contact']}: {m.get('tel') or S['ask_office']})",
        f"• {S['grade']}: {m.get('grade')}",
        f"• {S['reason']}: {m.get('reason') or '-'}",
    ]
    others = matches[1 : 1 + TOP_OTHERS]
    if others:
        lines += ["", "■ " + _fill(S["others_text"], n=len(others))]
        for o in others:
            lines.append(
                f"  - {o.get('name')}/{o.get('color')} ({S['score_badge']} {o.get('grade')}) · "
                f"{o.get('region') or '-'} · {S['held_short']}:{o.get('dep_place') or '-'}"
            )
    lines += [
        "",
        S["claim"],
        _fill(S["cta_text"], url=link or _site(lost)),
        _fill(S["official"], lost112=POLICE_FOUND_SEARCH),
        f"({S['footer']})",
    ] + ([f"{S['unsub']}: {unsub}"] if unsub else [])
    return "\n".join(lines)


def _card_html(m: dict, primary: bool = False, S: dict = KO) -> str:
    e = _html.escape
    img = _valid_img(m.get("image_url", ""))
    size = 120 if primary else 60
    if img.startswith("https://minwon24.police.go.kr/"):  # 서버가 줄인 작은 사진(원본은 한 장 최대 ~3MB)
        img = f"{settings.public_base_url.rstrip('/')}/thumb.jpg?u={_quote(img, safe='')}"
    imgtag = (
        f'<img src="{e(img)}" alt="" style="width:{size}px;height:{size}px;object-fit:cover;'
        f'border-radius:8px;border:1px solid #eee;margin-right:12px" />'
        if img else ""
    )
    g = m.get("grade")
    badge = (
        f'<span style="background:{_grade_color(g)};color:#fff;border-radius:6px;'
        f'padding:1px 8px;font-weight:700;font-size:12px">{e(S["score_badge"])} {e(str(g))}</span>'
    )
    reason = (
        f'<div style="font-size:13px;color:#374151;margin-top:4px">💬 {e(str(m.get("reason") or ""))}</div>'
        if primary and m.get("reason") else ""
    )
    org = (
        f'<div style="font-size:13px;color:#166534;margin-top:4px">'
        f'🏛 {e(str(m.get("org_name") or "-"))} · ☎ {e(str(m.get("tel") or S["ask_office"]))}</div>'
        if (m.get("org_name") or m.get("tel")) else ""
    )
    desc = (
        f'<div style="font-size:13px;color:#374151;margin-top:4px">📝 {e(_clean_desc(m.get("description"))[:140])}</div>'
        if primary and _clean_desc(m.get("description")) else ""
    )
    return (
        f'<div style="display:flex;align-items:flex-start;border:1px solid #eee;border-radius:10px;'
        f'padding:12px;margin:8px 0">{imgtag}<div style="flex:1">'
        f'<div style="font-weight:700">{e(str(m.get("name")))} '
        f'<span style="color:#6b7280;font-weight:400">/ {e(str(m.get("color")))}</span> {badge}</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-top:2px">{e(str(m.get("category") or ""))} · '
        f'{e(S["held_short"])}:{e(str(m.get("dep_place") or "-"))} · {e(str(m.get("region") or "-"))} · '
        f'{e(str(m.get("found_at") or "-"))}</div>{reason}{desc}{org}</div></div>'
    )


def _html_body(lost: dict, matches: list[dict], weak: bool = False, S: dict = KO, link: str = "",
               unsub: str = "") -> str:
    e = _html.escape
    site = _site(lost)
    cta_href = link or site
    head, desc = _intro(lost, weak, S)
    others = matches[1 : 1 + TOP_OTHERS]
    others_html = ""
    if others:
        others_html = (
            f'<h3 style="font-size:14px;color:#374151;margin:16px 0 4px">{e(_fill(S["others_html"], n=len(others)))}</h3>'
            + "".join(_card_html(o, S=S) for o in others)
        )
    logo = (f'<img src="cid:lk-logo" width="40" height="40" alt="{e(settings.brand_name)}" '
            'style="border-radius:9px;display:block" />') if _LOGO.exists() else ""
    site_link = f'<a href="{e(cta_href)}" style="color:#2563eb">{e(site)}</a>'
    lost112 = f'<a href="{POLICE_FOUND_SEARCH}">minwon24.police.go.kr</a>'
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Malgun Gothic\',sans-serif;'
        'max-width:560px;color:#1f2430">'
        # 브랜드 헤더 (로고 + 이름)
        '<div style="display:flex;align-items:center;gap:10px;padding-bottom:14px;'
        'border-bottom:1px solid #eee;margin-bottom:16px">'
        f'{logo}<span style="font-size:20px;font-weight:800;color:#1d4ed8">{e(settings.brand_name)}</span>'
        f'<span style="font-size:12px;color:#6b7280">{e(S["tagline"])}</span></div>'
        f'<h2 style="font-size:18px;margin:0 0 8px">{e(head)}</h2>'
        f'<p style="margin:0 0 12px">{e(desc)}</p>'
        f'{_card_html(_top(matches), primary=True, S=S)}{others_html}'
        # 사이트 이동 CTA
        f'<div style="text-align:center;margin:22px 0 6px">'
        f'<a href="{e(cta_href)}" style="display:inline-block;background:#2563eb;color:#fff;'
        'text-decoration:none;padding:13px 30px;border-radius:10px;font-weight:700;font-size:15px">'
        f'{e(S["cta"])}</a></div>'
        f'<p style="text-align:center;font-size:12px;color:#6b7280;margin:0 0 16px">'
        f'{_fill(e(S["or_type"]), url=site_link)}</p>'
        '<p style="color:#888;font-size:12px;margin-top:16px;border-top:1px solid #eee;padding-top:12px">'
        f'{_fill(e(S["official"]), lost112=lost112)} {e(S["footer"])}'
        + (f' · <a href="{e(unsub)}" style="color:#6b7280">{e(S["unsub"])}</a>' if unsub else "")
        + '</p></div>'
    )


def build_email(lost: dict, matches: list[dict], weak: bool = False, link: str = "",
                unsub: str = "") -> tuple[str, str, str]:
    """(제목, 텍스트, HTML) — 신고자 언어(lost['lang'])로. 한국어가 아니면 문구·물품 정보를 번역.

    link: 그 신고의 결과로 바로 가는 링크(report_link). 없으면 사이트 첫 화면.
    """
    lang = gemini.normalize_lang(lost.get("lang"))
    S = _ui(lang)
    ms = _localize(matches, lang)
    kind = "weak" if weak else "strong"
    subject = f"[{settings.brand_name}] {S[f'subj_{kind}']} — {_top(ms).get('name')}"
    return subject, _text_body(lost, ms, weak, S, link, unsub), _html_body(lost, ms, weak, S, link, unsub)


def _new_report_link(lost: dict) -> str:
    """메일마다 새 링크 토큰 발급(서버엔 해시만). 저장 실패해도 메일은 첫 화면 링크로 보냄."""
    try:
        from apps import store
        return report_link(lost, store.new_link_token(lost["id"]))
    except Exception as e:  # noqa: BLE001
        _emit(f"[알림] 결과 링크 발급 실패({type(e).__name__}) → 첫 화면 링크로")
        return _site(lost)


def _new_unsub_url(lost: dict) -> str:
    """'알림 그만 받기' 주소 — 수신 거부 전용 토큰(알림 끄기 외 권한 없음). 실패하면 빈 문자열(링크·헤더 생략)."""
    try:
        from apps import store
        return f"{settings.public_base_url.rstrip('/')}/unsubscribe?r={lost['id']}.{store.new_unsub_token(lost['id'])}"
    except Exception as e:  # noqa: BLE001
        _emit(f"[알림] 수신 거부 링크 발급 실패({type(e).__name__}) → 생략")
        return ""


class EmailNotifier:
    """SMTP 이메일 알림(HTML+텍스트). 신고자 언어로 발송. 발송 실패해도 요청을 죽이지 않음(로그 후 진행)."""

    def notify(self, lost: dict, matches: list[dict], weak: bool = False) -> None:
        # 수신자 = 신고자가 확인한 이메일만(관리자 주소로 대신 보내지 않음 — 남의 신고 내용이 새지 않게)
        to = (lost.get("email") or "").strip()
        if not (settings.smtp_user and settings.smtp_password and to) or not matches:
            _emit("[알림] 이메일 설정/수신자/후보 미비 → 발송 생략")
            return
        unsub = _new_unsub_url(lost)
        subject, text, html = build_email(lost, matches, weak, link=_new_report_link(lost), unsub=unsub)
        # 원클릭 수신 거부(RFC 8058) — Gmail·Yahoo가 대량 발송자에게 요구, 소량이어도 스팸 판정에 유리
        headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if unsub else {}
        if self._send(to, subject, text, html, logo=True, headers=headers):
            _emit(f"📧 [이메일 알림→{to}, {lost.get('lang') or 'ko'}] '{_top(matches).get('name')}' "
                  f"외 {max(len(matches)-1,0)}건 발송 완료")

    def verify(self, to: str, lang: str, link: str) -> None:
        if not (settings.smtp_user and settings.smtp_password and to):
            _emit("[알림] 이메일 설정/수신자 미비 → 확인 메일 생략")
            return
        subject, text, html = build_verify_email(lang, link)
        if self._send(to, subject, text, html):
            _emit(f"✉️ [확인 메일→{to}, {lang}] 발송 완료")

    @staticmethod
    def _send(to: str, subject: str, text: str, html: str, logo: bool = False, headers: dict | None = None) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        # 보내는 주소: SMTP_FROM(예: alerts@lostkorea.com) — 없으면 예전처럼 SMTP 계정 주소
        msg["From"] = formataddr((settings.brand_name, settings.smtp_from or settings.smtp_user))
        msg["To"] = to
        if settings.smtp_reply_to:
            msg["Reply-To"] = settings.smtp_reply_to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        if logo and _LOGO.exists():  # HTML 파트에 로고 인라인 첨부(cid:lk-logo)
            try:
                html_part = msg.get_payload()[-1]
                html_part.add_related(_LOGO.read_bytes(), "image", "png", cid="lk-logo")
            except Exception:  # noqa: BLE001
                pass
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
                s.starttls()
                s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001
            _emit(f"[알림] 이메일 발송 실패({type(e).__name__}: {e}) → 스킵")
            return False


def get_notifier() -> Notifier:
    """config의 notifier_channel로 알림 채널 선택. email 미설정 시 console 폴백."""
    if settings.notifier_channel == "email":
        if settings.smtp_user and settings.smtp_password:
            return EmailNotifier()
        _emit("[알림] NOTIFIER_CHANNEL=email 이지만 SMTP 미설정 → console로 폴백")
    return ConsoleNotifier()


# 주입 지점 — config로 채널 선택(추후 KakaoNotifier 등 추가 가능)
notifier: Notifier = get_notifier()
