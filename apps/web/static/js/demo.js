// 첫 화면 '이렇게 찾아드려요' 시연 — 입력 → AI 검색 → 알림 신청 → 다음 날 메일 → 수령 안내를 25초 동안 재생.
// - 화면 폭 1000px 이상이면 브라우저 창(PC 화면·PC 메일), 그보다 좁으면 휴대폰 틀. 장면·시계는 같고 틀·배치만 다름.
// - 영상이 아니라 HTML로 그림(가볍고, 글자가 선명하고, 언어가 바뀌면 같이 바뀜). 문구는 실제 화면·알림 메일 문구를 줄여 씀.
// - 모든 장면은 시계 t 하나로 계산(render(t)) → 장(章) 이동·일시정지·반복이 어긋나지 않음.
// - 화면에 30% 이상 보일 때만 재생, 탭이 숨으면 멈춤. '움직임 줄이기' 설정이면 자동 재생 없이 메일 장면 한 장만.
// - 시연 틀은 aria-hidden(타자 글자를 화면 낭독기가 읽지 않게). 단계 목록이 같은 내용을 글로 전함.
(function () {
  const screen = document.getElementById("flowDemo");
  const device = document.getElementById("flowDevice");
  if (!screen || !device || window.LK_VIEW === "browse") return;
  const wide = window.matchMedia("(min-width: 1000px)");
  const KO = window.LK_LANG !== "en";
  const S = KO ? {
    ask: "무엇을 잃어버렸나요?", ph: "언제·어디서·무엇을 잃어버렸는지 적어 주세요",
    query: "어제 홍대입구역 근처에서 검정 가죽 지갑을 잃어버렸어요",
    photo: "사진 추가", date: "분실일", find: "찾기", searching: "찾는 중…",
    steps: ["설명 이해하기", "잃어버린 지역 넓히기", "검색어 만들기", "습득물 검색", "후보 하나씩 비교"],
    open: "지금은 딱 맞는 게 없어요. 새 습득물이 들어오면 자동으로 다시 찾아 알려드려요.",
    weakName: "검정 반지갑", weakColor: "검정", weakMeta: "보관: 서울영등포경찰서",
    alertT: "알림 설정", alertD: "유력 후보가 나오면 이메일로 알려드려요.",
    email: "me@example.com", emailPh: "이메일 주소", save: "저장", period: "알림 기간 30일", weakOpt: "비슷한 후보도 참고로 알림 받기",
    verify: "확인 메일을 보냈어요. 메일의 [알림 받기 확인]을 눌러야 알림이 켜져요.",
    day: "다음 날 · 새 습득물이 들어왔어요", notiApp: "메일 · 지금",
    subj: "[LostKorea] 분실물 매칭 알림 — 검정 가죽 지갑", tagline: "분실물 매칭 AI",
    inbox: "받은편지함", toMe: "나에게", mailTab: "메일", folders: ["받은편지함", "별표", "보낸편지함"],
    nav: ["분실물 찾기", "전체 습득물", "이용 방법"],
    head: "유력 후보를 찾았어요 — 확인해보세요",
    desc: "'어제 홍대입구역 근처에서 검정 가죽 지갑을 잃어버렸어요' 신고와 일치할 가능성이 높은 습득물을 찾았어요.",
    item: "검정 가죽 지갑", color: "검정", score: "일치",
    reason: "색·종류가 같고, 잃어버린 홍대입구역 근처(마포구)에서 발견됐어요.",
    org: "서울마포경찰서", tel: "02-3149-6133", cta: "LostKorea에서 전체 결과 보기 →",
    held: "보관", mine: "내 물건이에요", notMine: "아니에요",
    claimDone: "확인 완료! 아래 보관기관으로 수령을 문의하세요.",
    claim: ["보관기관에 전화로 문의", "여권·신분증 지참해 직접 방문", "지도로 위치 확인"],
  } : {
    ask: "What did you lose?", ph: "What, where and when — any language",
    query: "Lost a black leather wallet near Hongdae Station yesterday",
    photo: "Add photo", date: "Date lost", find: "Find", searching: "Searching…",
    steps: ["Understanding your description", "Mapping where it may have gone", "Preparing searches", "Searching found items", "Checking each candidate"],
    open: "No exact match yet. We'll re-check automatically as new items arrive.",
    weakName: "Black bifold wallet", weakColor: "black", weakMeta: "Held at: Yeongdeungpo Police Station",
    alertT: "Alert settings", alertD: "We'll email you when a strong candidate appears.",
    email: "me@example.com", emailPh: "Email address", save: "Save", period: "Alert for 30 days", weakOpt: "Also alert me about similar candidates",
    verify: "We sent a confirmation email. Press Confirm in it to turn on alerts.",
    day: "Next day · new items came in", notiApp: "Mail · now",
    subj: "[LostKorea] Lost item match alert — Black leather wallet", tagline: "Lost item matching AI",
    inbox: "Inbox", toMe: "to me", mailTab: "Mail", folders: ["Inbox", "Starred", "Sent"],
    nav: ["Find my item", "Browse", "How it works"],
    head: "We found a strong candidate — take a look",
    desc: "We found an item that likely matches your report 'Lost a black leather wallet near Hongdae Station yesterday'.",
    item: "Black leather wallet", color: "black", score: "Match",
    reason: "Same color and type, found near Hongdae Station (Mapo-gu), where you lost it.",
    org: "Seoul Mapo Police Station", tel: "02-3149-6133", cta: "See all results on LostKorea →",
    held: "Held at", mine: "This is mine", notMine: "Not mine",
    claimDone: "Confirmed! Contact the holding office below to retrieve it.",
    claim: ["Call the holding office", "Visit with your passport/ID", "Check the location on the map"],
  };
  const svg = (d, cls = "") => `<svg class="fd-ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const IC = {
    cam: '<path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/>',
    cal: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    bell: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>',
    check: '<path d="m5 12 4 4 10-10"/>',
    spark: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    bldg: '<path d="M4 21V8l8-5 8 5v13"/><path d="M9 21v-6h6v6M4 21h16"/>',
    phone: '<path d="M5 4h3l2 5-2.5 1.5a11 11 0 0 0 6 6L15 14l5 2v3a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
    wallet: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M16 12h5v4h-5a2 2 0 0 1 0-4zM6 6l9-3 1 3"/>',
  };
  const logo = '<span class="fd-logo"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg></span>';

  let desk = false;
  function build() {
  desk = wide.matches;
  device.className = "device " + (desk ? "browser" : "phone");
  const top = desk
    ? `<div class="fd-chrome"><span class="fd-dots"><i></i><i></i><i></i></span>
         <span class="fd-tab fd-tab-site">${logo}LostKorea</span><span class="fd-tab fd-tab-mail">${svg(IC.mail)}${S.mailTab}</span>
         <span class="fd-url"><b class="fd-url-site">lostkorea.com</b><b class="fd-url-mail">mail.example.com</b></span></div>`
    : `<div class="fd-status"><b>9:41</b><span class="fd-island"></span><i></i></div>`;
  screen.innerHTML = `${top}
    <div class="fd-bar">${logo}<b>Lost<span>Korea</span></b>${S.nav.map(n => `<span class="fd-nav">${n}</span>`).join("")}</div>
    <div class="fd-l fd-input">
      <p class="fd-h">${S.ask}</p>
      <div class="fd-ta"><span class="fd-ph">${S.ph}</span><span class="fd-typed"></span><i class="fd-caret"></i></div>
      <div class="fd-row"><span class="fd-chip">${svg(IC.cam)}${S.photo}</span><span class="fd-chip">${svg(IC.cal)}${S.date}</span></div>
      <div class="fd-btn fd-go">${S.find}</div>
    </div>
    <div class="fd-l fd-prog">
      <div class="fd-quote">${S.query}</div>
      <p class="fd-h">${S.searching}</p>
      <ul class="fd-steps">${S.steps.map(s => `<li><span class="fd-dot">${svg(IC.check)}</span>${s}</li>`).join("")}</ul>
    </div>
    <div class="fd-l fd-res">
      <div class="fd-banner fd-a">${svg(IC.clock)}<span>${S.open}</span></div>
      <div class="fd-card fd-a"><span class="fd-grade g-gray">41</span><div><b>${S.weakName}</b> <em>/ ${S.weakColor}</em><small>${S.weakMeta}</small></div></div>
      <div class="fd-alert fd-a">
        <b>${svg(IC.bell)}${S.alertT}</b><p>${S.alertD}</p>
        <div class="fd-email"><span class="fd-ph">${S.emailPh}</span><span class="fd-typed"></span><i class="fd-caret"></i></div>
        <div class="fd-arow"><span class="fd-sel">${S.period} ▾</span><div class="fd-btn fd-save">${S.save}</div></div>
        <span class="fd-chk"><i></i>${S.weakOpt}</span>
        <p class="fd-msg">${S.verify}</p>
      </div>
    </div>
    <div class="fd-l fd-mail">
      <aside class="fd-side">${S.folders.map((f, i) => `<span class="${i ? "" : "on"}">${f}${i ? "" : " <b>1</b>"}</span>`).join("")}
        <div class="fd-item"><b>LostKorea</b><span>${S.subj}</span></div></aside>
      <div class="fd-mtop fd-a"><span>‹ ${S.inbox}</span><b>${S.subj}</b>
        <div class="fd-from"><span class="fd-av">L</span><div><b>LostKorea</b><small>${S.toMe}</small></div></div></div>
      <div class="fd-mbrand fd-a">${logo}<b>LostKorea</b><small>${S.tagline}</small></div>
      <h4 class="fd-a">${S.head}</h4>
      <p class="fd-a">${S.desc}</p>
      <div class="fd-mcard fd-a"><span class="fd-mimg">${svg(IC.wallet)}</span><div>
        <b>${S.item}</b> <span class="fd-badge">${S.score} 88</span>
        <small>${svg(IC.spark)}${S.reason}</small>
        <small class="fd-org">${svg(IC.bldg)}<span>${S.org} · <span class="fd-nw">${S.tel}</span></span></small></div></div>
      <div class="fd-btn fd-cta fd-a">${S.cta}</div>
    </div>
    <div class="fd-l fd-claim">
      <div class="fd-match fd-a"><span class="fd-grade g-green">88<small>${S.score}</small></span><div>
        <b>${S.item}</b> <em>/ ${S.color}</em><small>${S.held}: ${S.org}</small>
        <div class="fd-reason">${svg(IC.spark)}<span>${S.reason}</span></div>
        <div class="fd-acts"><span class="fd-btn fd-mine">${svg(IC.check)}${S.mine}</span><span class="fd-btn fd-out">${S.notMine}</span></div>
      </div></div>
      <div class="fd-done fd-a">
        <div class="fd-banner ok">${svg(IC.check)}<span>${S.claimDone}</span></div>
        <ol>${S.claim.map((c, i) => `<li class="fd-a">${c}${i === 0 ? ` <u>${S.tel}</u>` : ""}</li>`).join("")}</ol>
      </div>
    </div>
    <div class="fd-dim"></div>
    <div class="fd-day">${S.day}</div>
    <div class="fd-noti"><span class="fd-app">${svg(IC.mail)}</span><div><small>${S.notiApp}</small><b>LostKorea</b><span>${S.subj}</span></div></div>
    <div class="fd-cursor"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3l14 8-6 1.6L10 19z"/></svg></div>`;
  }
  build();

  // ── 타임라인(초) ──
  const END = 25;
  const CH = [0, 4.5, 8, 13, 20];            // 장 시작: 설명 입력 · AI 검색 · 알림 신청 · 메일 도착 · 수령 안내
  const STILL = [3.8, 7.6, 12.4, 17.6, 24];  // 장마다 '다 보인' 순간(움직임 줄이기 설정에서 장을 고르면 이 장면)
  const LAYERS = [[".fd-input", 0, 4.6], [".fd-prog", 4.5, 8.1], [".fd-res", 8, 15.6], [".fd-mail", 15.5, 20.1], [".fd-claim", 20, END + 1]];
  const STEP_DONE = [5.0, 5.5, 6.0, 6.6, 7.3];
  const MOVES = [[0.2, 0.9, ".fd-input .fd-ta"], [3.6, 4.2, ".fd-go"], [8.3, 8.8, ".fd-email"], [10.3, 10.7, ".fd-save"],
                 [14.5, 15.1, ".fd-noti"], [18.5, 19.1, ".fd-cta"], [20.5, 21.1, ".fd-mine"], [22.4, 23.2, null]];
  const CLICKS = [[4.3, ".fd-go"], [8.85, ".fd-email"], [10.8, ".fd-save"], [15.2, ".fd-noti"], [19.2, ".fd-cta"], [21.2, ".fd-mine"]];

  const $s = (sel) => screen.querySelector(sel);
  const $all = (sel) => [...screen.querySelectorAll(sel)];
  const clamp = (x) => Math.min(1, Math.max(0, x));
  const prog = (t, a, b) => clamp((t - a) / (b - a));
  const ease = (x) => x < .5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
  const show = (el, p, dy = 8) => {  // 나타나기: 투명도 + 살짝 위로
    el.style.opacity = p;
    el.style.transform = p < 1 ? `translateY(${((1 - ease(p)) * dy).toFixed(1)}px)` : "";
  };
  const type = (box, text, t, a, b) => {
    const n = Math.round(text.length * prog(t, a, b));
    box.querySelector(".fd-typed").textContent = text.slice(0, n);
    box.querySelector(".fd-ph").style.display = n ? "none" : "";
    const focus = t > a - .5 && t < b + .6;  // 입력칸을 누른 뒤 ~ 다 친 직후: 타자 중엔 켜 두고 그 외엔 깜빡임
    box.querySelector(".fd-caret").style.opacity = focus && ((t >= a && t <= b) || Math.floor(t * 2.5) % 2 === 0) ? 1 : 0;
  };
  const center = (sel) => {
    const s = screen.getBoundingClientRect(), r = $s(sel).getBoundingClientRect();
    return [r.left - s.left + r.width * .62, r.top - s.top + r.height * .58];
  };
  const rest = () => [screen.clientWidth * .78, screen.clientHeight * .9];

  function cursorAt(t) {
    let pos = rest();
    for (const [a, b, sel] of MOVES) {
      if (t < a) break;
      const to = sel ? center(sel) : rest();
      if (t >= b) { pos = to; continue; }
      const k = ease(prog(t, a, b));
      return [pos[0] + (to[0] - pos[0]) * k, pos[1] + (to[1] - pos[1]) * k];
    }
    return pos;
  }

  function render(t) {
    // 반복 이음새: 끝 0.4초 어두워졌다가 처음 0.3초 밝아짐
    const fadeIn = t <= 0 ? 1 : prog(t, 0, .3);
    screen.style.setProperty("--wash", (1 - Math.min(fadeIn, 1 - prog(t, END - .4, END))).toFixed(3));
    for (const [sel, a, b] of LAYERS) {
      const el = $s(sel);
      const o = a === 0 ? 1 - prog(t, b - .3, b) : Math.min(prog(t, a, a + .3), 1 - prog(t, b - .3, b));
      el.style.opacity = o; el.style.visibility = o > 0 ? "visible" : "hidden";
    }
    // 1) 설명 입력
    type($s(".fd-input .fd-ta"), S.query, t, .9, 3.4);
    // 2) AI 검색 — 단계가 차례로 체크
    $all(".fd-steps li").forEach((li, i) => {
      const done = t >= STEP_DONE[i], on = !done && t >= (i ? STEP_DONE[i - 1] : 4.7);
      li.className = done ? "done" : on ? "on" : "";
      show(li, prog(t, 4.6 + i * .12, 4.9 + i * .12), 6);
    });
    // 3) 결과(약한 후보) + 알림 신청
    $all(".fd-res .fd-a").forEach((el, i) => show(el, prog(t, 8 + i * .2, 8.4 + i * .2)));
    type($s(".fd-email"), S.email, t, 8.9, 10.2);
    show($s(".fd-msg"), prog(t, 11, 11.35), 4);
    $s(".fd-dim").style.opacity = Math.min(prog(t, 13, 13.4), 1 - prog(t, 15.4, 15.6));
    // 4) 다음 날 → 메일 알림 → 메일
    const day = Math.min(prog(t, 13, 13.3), 1 - prog(t, 14.3, 14.6));
    $s(".fd-day").style.opacity = day;
    const n = prog(t, 14, 14.45), nOut = prog(t, 15.4, 15.6);
    $s(".fd-noti").style.transform = `translateY(${((1 - ease(n)) * -120).toFixed(1)}%)`;
    $s(".fd-noti").style.opacity = n > 0 ? 1 - nOut : 0;
    $all(".fd-mail .fd-a").forEach((el, i) => show(el, prog(t, 15.6 + i * .15, 16 + i * .15)));
    if (desk) screen.classList.toggle("in-mail", t >= 15.5 && t < 20.05);
    // 5) 수령 안내
    $all(".fd-claim > .fd-a").forEach((el, i) => show(el, prog(t, i ? 21.4 : 20, i ? 21.8 : 20.4)));
    $all(".fd-done li").forEach((el, i) => show(el, prog(t, 21.7 + i * .3, 22 + i * .3), 4));
    // 누름 효과 + 커서
    for (const [c, sel] of CLICKS) $s(sel).classList.toggle("is-press", t >= c && t < c + .22);
    const [x, y] = cursorAt(t);
    const cur = $s(".fd-cursor");
    cur.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    cur.classList.toggle("press", CLICKS.some(([c]) => t >= c && t < c + .28));
    // 옆 단계 목록: 지금 장 강조 + 진행 막대
    const idx = CH.reduce((k, c, i) => (t >= c ? i : k), 0);
    const end = CH[idx + 1] ?? END;
    steps.forEach((li, i) => {
      li.classList.toggle("is-on", i === idx);
      li.style.setProperty("--p", i === idx ? prog(t, CH[idx], end).toFixed(3) : i < idx ? 1 : 0);
    });
  }

  // ── 재생 제어 ──
  const steps = [...document.querySelectorAll("#flowSteps li")];
  const toggle = document.getElementById("flowToggle");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fixed = parseFloat(new URLSearchParams(location.search).get("flowT"));  // ?flowT=12.4 → 그 순간에 멈춘 장면(점검·캡처용)
  let t = reduce ? STILL[3] : 0, playing = !reduce, inView = !("IntersectionObserver" in window), last = 0, raf = 0;
  if (fixed >= 0 && fixed < END) { t = fixed; playing = false; }
  if (reduce) screen.classList.add("no-cursor");
  const running = () => playing && inView && !document.hidden;
  function tick(now) {
    raf = 0;
    t += last ? Math.min(.1, (now - last) / 1000) : 0;
    last = now;
    if (t >= END) t -= END;
    render(t);
    if (running()) raf = requestAnimationFrame(tick); else last = 0;
  }
  const kick = () => { if (running() && !raf) { last = 0; raf = requestAnimationFrame(tick); } };
  const label = () => {
    if (!toggle) return;
    toggle.textContent = playing ? toggle.dataset.pause : toggle.dataset.play;
    toggle.setAttribute("aria-pressed", playing ? "false" : "true");
  };
  toggle?.addEventListener("click", () => {
    playing = !playing;
    if (playing) screen.classList.remove("no-cursor");
    else if (raf) { cancelAnimationFrame(raf); raf = 0; }  // 누른 그 장면에서 바로 멈춤
    label(); kick();
  });
  steps.forEach((li, i) => li.querySelector("button")?.addEventListener("click", () => {
    t = playing ? CH[i] + .01 : STILL[i];
    render(t); kick();
  }));
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => { inView = e.isIntersecting; kick(); }, { threshold: .3 }).observe(screen);
  }
  document.addEventListener("visibilitychange", kick);
  window.addEventListener("resize", () => render(t));
  wide.addEventListener?.("change", () => { build(); render(t); });  // PC ↔ 휴대폰 폭이 바뀌면 틀을 다시
  label();
  render(t);
  kick();
})();
