// LostKorea 앱 — 검색(실시간 진행)·결과·알림 설정·내 신고·전체 습득물. 페이지 글자는 서버가 언어별로 채우고,
// 여기 I18N은 검색 중·결과처럼 동적으로 바뀌는 문구만. 언어는 서버가 window.LK_LANG로 알려줌.
const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").toString().replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const gradeColor = (g) => g == null ? 'var(--gray)' : g >= 80 ? 'var(--green)' : g >= 50 ? 'var(--amber)' : 'var(--gray)';

// 선 아이콘(사이트 공통 스타일 — 이모지 대신)
const IC = {
  pin: '<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
  phone: '<path d="M5 4h3l2 5-2.5 1.5a11 11 0 0 0 6 6L15 14l5 2v3a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/>',
  building: '<path d="M4 21V8l8-5 8 5v13"/><path d="M9 21v-6h6v6M4 21h16"/>',
  note: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>',
  spark: '<path d="M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/>',
  check: '<path d="m5 12 4 4 10-10"/>',
  bell: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  map: '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
  link: '<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
  speech: '<path d="M4 5h16v11H8l-4 4z"/>',
};
const ic = (name, cls = "ic") => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${IC[name]}</svg>`;

// ===== 다국어(i18n) — 외국인 접근성. 검색은 언어 무관(LLM이 한국어로 정규화), UI·안내·판정근거만 전환 =====
const I18N = {
  ko: {
    heroSub:"전국 경찰서·유실물센터에 들어온 습득물을 <b>말이나 사진으로</b> 찾아요. 어떤 언어로 써도 돼요.",
    pickPhoto:"사진 추가", photoRemove:"사진 빼기", photoTipTitle:"사진으로 찾을 때 알아두세요",
    scoreLabel:"일치", scoreLegend:"점수 = AI가 추정한 같은 물건일 가능성 (80↑ 유력 · 50~79 비슷함)",
    howSearched:"AI가 이렇게 찾았어요", howItWorks:"이렇게 동작해요",
    step_extract:"설명 이해하기", step_extractImg:"사진 살펴보기", step_route:"잃어버린 지역 넓히기",
    step_fanout:"검색어 만들기", step_search:"습득물 검색", step_grade:"후보 하나씩 비교", step_visual:"사진 대조",
    kicker:"🔍 분실물 매칭 AI 에이전트", heroTitle:"잃어버린 물건, AI가 찾아드려요",
    cta:"지금 찾아보기 →",
    f1t:"자연어·사진 검색", f1d:"\"어제 2호선에서 검정 지갑 잃어버렸어요\"처럼 말하듯, 또는 사진 한 장으로.",
    f2t:"의미 기반 매칭 + 근거", f2d:"키워드가 아니라 뜻으로 찾고(한↔영·오타 커버), 왜 당신 물건일지 근거를 제시.",
    f3t:"경로 기반 지역", f3d:"지하철·버스로 이동한 경유 지역까지 넓게 살펴 순위에 반영.",
    f4t:"외국인도 OK", f4d:"영어 화면 + 어떤 언어로 검색해도 결과. 수령 방법·지도·통역 안내까지.",
    step1:"<b>1.</b> 말로 설명하거나 사진 올리기", step2:"<b>2.</b> AI가 후보 + 근거 제시 (확인은 본인이)",
    step3:"<b>3.</b> 내 물건이면 보관기관·연락처·지도 안내",
    registerLead:"무슨 물건을, <b>어디서·언제</b> 잃어버렸는지 자유롭게 적으면 AI가 습득물에서 찾아드려요. 자세할수록 정확해요.",
    exLabel:"예시로 바로 해보기:",
    mineLead:"딱 맞는 게 없어도 신고는 저장돼요. <b>새 습득물이 들어오면 자동으로 다시 찾아</b> 이메일로 알려드려요. 내 신고는 이 브라우저에서만 보여요.",
    footer:'<a href="/privacy">개인정보처리방침</a> · <a href="/terms">이용약관</a> · <a href="https://github.com/cwal8202/lostkorea/issues" target="_blank" rel="noopener">문의</a>',
    heroStat:(n, d)=>`지금 <b>${n}</b>건의 습득물에서 찾는 중 · 최신 습득물 <b>${d}</b> · 매일 <b>00시(KST)</b> 자동 갱신`,
    catPrefix:"분류:", zoom:"클릭하면 크게 보기", searching:"찾는 중…",
    naverMap:"네이버 지도", googleMap:"구글 지도",
    helpline:"외국인이라 통화가 어렵다면 <b>1330 관광통역안내전화</b>(24시간)로 전화하면 보관기관과의 통화를 통역해줘요.",
    lost112:"(경찰청 유실물 통합포털)",
    xlateFail:"번역 서비스(Google)에 연결하지 못했어요. 영어 화면을 이용해 주세요.",
    subtitle:"잃어버린 물건을 자연어로 적으면, 습득물 공개데이터에서 찾아드려요.",
    tabFind:"🔍 AI로 찾기", tabBrowse:"📋 전체 둘러보기", register:"분실물 찾기",
    textPh:"예: 어제 2호선 지하철에서 검정색 닥스 지갑을 잃어버렸어요. 안에 신분증이랑 카드가 있었어요.",
    emailPh:"알림 받을 이메일 (선택) — 나중에 매칭되면 여기로 보내드려요",
    notifyWeak:"정확도가 낮은 <b>비슷한 후보</b>도 참고로 알림 받기 (이메일 입력 시)",
    lostDate:"분실일(선택)", find:"찾기", orPhoto:"— 또는 사진으로 찾기 —",
    searchPhoto:"사진으로 찾기",
    matchResult:"매칭 결과", myReports:"내 신고 (이 브라우저)", refresh:"새로고침", loading:"불러오는 중…",
    browseTitle:"전체 습득물 둘러보기", browseDesc:"공개데이터로 수집된 습득물을 키워드·지역·날짜로 필터링해 살펴보세요. (AI 판정 없이 즉시 조회)",
    kwPh:"키워드 (예: 지갑, 에어팟, 카드)", allRegions:"전체 지역", foundDate:"습득일", searchBtn:"검색", reset:"초기화",
    stepWorking:"진행 중…", total:"전체",
    regionCount:(n)=>`${n}개 지역까지 넓혀 가점`, noRegion:"장소 정보 없음 — 전국에서 검색",
    searchCount:(n)=>`후보 ${n}건 — 아래 목록에 AI가 점수·근거를 채워요`,
    gradeDone:(g)=>`채점 완료 — 최고 ${g}점`, visualDone:(n)=>`사진 있는 후보 ${n}건과 직접 비교`,
    streamFail:"연결이 끊겼어요. 다시 시도해주세요.",
    visualSim:"사진도 유사",
    needText:"분실물 설명을 입력하세요.", needPhoto:"먼저 사진을 선택하세요.", errPrefix:"오류: ", serverErr:"서버 오류 ",
    invalidInput:"분실물 설명이 아닌 것 같아요. <b>무슨 물건을, 어디서·언제</b> 잃어버렸는지 적어주세요. (예: 어제 서울 2호선에서 검정 지갑 잃어버렸어요)",
    bannerCand:"유력 후보를 찾았어요. 아래에서 <b>맞는지 확인</b>해주세요. (확정은 본인이!)",
    bannerOpen:"지금은 딱 맞는 게 없어요. 접수해두고 새 습득물이 들어오면 자동으로 다시 찾아 알려드려요. (아래에 비슷한 후보가 있으면 확인해보세요)",
    extracted:"추출", fromPhoto:"사진에서 읽음", searchRegion:"검색 지역", candQueries:"후보 쿼리",
    candCount:(n)=>`후보 ${n}건 — 본인 물건이 있나요?`, noCand:"후보 없음",
    photoRead:"올려주신 사진에서 아래 정보를 읽어 검색했어요 👇",
    mine:"내 물건이에요", notMine:"아니에요", lblHeld:"보관", lblRegion:"지역", lblFound:"습득일", mapLink:"지도",
    claimDone:"확인 완료! 아래 보관기관으로 수령을 문의하세요.", claimHeldAt:"보관장소",
    claimNoOrg:"보관기관 정보 없음", claimTelFallback:"· 연락처는 아래 포털에서 조회",
    claimHow:"수령 방법", claimStep1:"보관기관에 전화로 문의", claimStep2:"여권·신분증 지참해 직접 방문(본인 확인)",
    claimStep3:"지도로 위치 확인", claimTimeline:"습득물은 보통 일정 기간(최대 6개월) 보관 후 처리될 수 있어요 — 되도록 빨리 문의하세요.",
    claimNote:"※ 신분 확인 등 기관 절차에 따라 본인이 수령하세요. LostKorea는 안내만 제공합니다.",
    alertTitle:"알림 설정",
    alertDesc:"지금 못 찾아도 괜찮아요. 새 습득물이 들어올 때마다 매일 다시 찾아보고, <b>유력 후보가 나오면 이메일</b>로 알려드려요. 이메일을 새로 남기면 지금 결과도 보내드려요.",
    alertPeriod:"알림 기간", days:(n)=>`${n}일`, save:"저장", saving:"저장 중…", alertSaved:"저장했어요.",
    verifySent:(e)=>`<b>${e}</b>로 확인 메일을 보냈어요. 메일의 <b>[알림 받기 확인]</b>을 눌러야 알림이 켜져요.`,
    tooMany:"요청이 너무 많아요. 잠시 후 다시 시도해주세요.",
    privacyNote:'이름·전화번호 등 개인정보는 적지 마세요. 신고 내용·사진은 매칭을 위해 Google Gemini로 분석되고(사진은 저장 안 함), 신고는 알림 기간이 끝나고 30일 뒤 자동 삭제돼요. 신고하면 <a href="/privacy" target="_blank">개인정보처리방침</a>·<a href="/terms" target="_blank">이용약관</a>에 동의한 것으로 봐요.',
    del:"삭제", delConfirm:"이 신고를 삭제할까요? 신고 내용·이메일이 바로 지워지고 알림도 끝나요.", delFail:"삭제하지 못했어요. 잠시 후 다시 시도해주세요.",
    alertUntil:(d, e)=> e ? `<b>${d}</b>까지 매일 비교해 <b>${e}</b>로 알려드려요.`
                          : `<b>${d}</b>까지 매일 다시 찾아요. <b>이메일을 남기면</b> 알림을 보내드려요.`,
    resultsSent:(x)=>`지금 결과도 ${x}로 보냈어요.`,
    emailPh2:"이메일 주소", emailInvalid:"유효한 이메일을 입력하세요.",
    browseXlate:"", perPage:"개씩", browseCount:(n)=>`총 ${n.toLocaleString()}건`, noItems:"조건에 맞는 습득물이 없어요.", noName:"(이름 없음)",
    prev:"‹ 이전", next:"다음 ›", loadFail:"불러오지 못했어요.", mineEmpty:"이 브라우저에서 등록한 신고가 없어요.",
    mineLoadFail:"목록을 불러오지 못했어요.", bestMatch:"최고 매칭",
    showMore:(n)=>`더 보기 (전체 ${n}건)`, collapse:"접기",
    stConfirmed:"확인됨", stCandidate:"유력 후보", stOpen:"찾는중", stExpired:"기간 만료",
    watchTo:(d)=>`알림 ~${d}`,
  },
  en: {
    heroSub:"Describe it <b>in any language</b> or add a photo — we search items handed in to police and lost-and-found offices across Korea.",
    pickPhoto:"Add photo", photoRemove:"Remove photo", photoTipTitle:"Searching with a photo — good to know",
    scoreLabel:"match", scoreLegend:"Score = AI's estimate that it's your item (80+ strong · 50–79 similar)",
    howSearched:"How we searched", howItWorks:"How it works",
    step_extract:"Understanding your description", step_extractImg:"Reading your photo", step_route:"Mapping where it may have gone",
    step_fanout:"Preparing searches", step_search:"Searching found items", step_grade:"Checking each candidate", step_visual:"Comparing photos",
    kicker:"🔍 AI lost & found for Korea", heroTitle:"Lost something in Korea? We'll help you find it.",
    cta:"Start searching →",
    f1t:"Any language, or a photo", f1d:"Write it the way you'd say it — \"I left my black wallet on the subway near Hongdae\" — or upload one photo.",
    f2t:"Matched by meaning, with reasons", f2d:"Item names are in Korean; we match across languages and typos and tell you why each one fits.",
    f3t:"Follows your route", f3d:"Items often travel along a subway or bus line. We also search the districts your route passes through.",
    f4t:"Help to get it back", f4d:"Where it's held, the phone number, maps, and a free interpreter line for the call.",
    step1:"<b>1.</b> Describe it or upload a photo", step2:"<b>2.</b> AI shows candidates + reasons (you decide)",
    step3:"<b>3.</b> Yours? Get the office, phone and map",
    registerLead:"Tell us <b>what</b> you lost, <b>where</b> and <b>when</b> — in any language. The more detail, the better.",
    exLabel:"Try an example:",
    mineLead:"No exact match yet? Your report is saved. <b>We re-check every day as new items arrive</b> and email you. Your reports are visible only in this browser.",
    footer:'<a href="/en/guide">Lost something in Korea? Guide</a> · <a href="/en/privacy">Privacy</a> · <a href="/en/terms">Terms</a> · <a href="https://github.com/cwal8202/lostkorea/issues" target="_blank" rel="noopener">Contact</a> · <a href="/ko">한국어</a>',
    heroStat:(n, d)=>`Searching <b>${n}</b> found items · newest <b>${d}</b> · updated daily at midnight (KST)`,
    catPrefix:"Category: ", zoom:"Click to enlarge", searching:"Searching…",
    naverMap:"Naver Map", googleMap:"Google Maps",
    helpline:"Don't speak Korean? Call <b>1330 (Korea Travel Helpline, 24/7)</b> — they can interpret your call with the holding office. From a foreign phone: +82-2-1330.",
    lost112:"(Korean National Police lost &amp; found portal, Korean only)",
    xlateFail:"Couldn't reach the translation service (Google). Please use the English page.",
    subtitle:"Describe your lost item in any language — we search Korea's public found-item data.",
    tabFind:"🔍 AI Search", tabBrowse:"📋 Browse all", register:"Find a lost item",
    textPh:"e.g., I lost a black wallet near Gangnam Station yesterday. — A station, area or landmark is enough; you don't need the subway line number or direction.",
    emailPh:"Email for alerts (optional) — we'll notify you when it matches",
    notifyWeak:"Also alert me about <b>weaker, similar candidates</b> (for reference; needs email)",
    lostDate:"Date lost (optional)", find:"Search", orPhoto:"— or search by photo —",
    searchPhoto:"Search by photo",
    matchResult:"Match results", myReports:"My reports (this browser)", refresh:"Refresh", loading:"Loading…",
    browseTitle:"Browse all found items", browseDesc:"Browse collected found items by keyword, region and date. (Instant — no AI grading.)",
    kwPh:"Keyword (e.g., wallet, AirPods, card)", allRegions:"All regions", foundDate:"Found date", searchBtn:"Search", reset:"Reset",
    stepWorking:"Working…", total:"Total",
    regionCount:(n)=>`Boosting ${n} districts along the route`, noRegion:"No place given — searching nationwide",
    searchCount:(n)=>`${n} candidates — AI is scoring them below`,
    gradeDone:(g)=>`Scored — best ${g}`, visualDone:(n)=>`Compared with ${n} candidate photo(s)`,
    streamFail:"Connection lost. Please try again.",
    visualSim:"Photo match",
    needText:"Please describe your lost item.", needPhoto:"Please choose a photo first.", errPrefix:"Error: ", serverErr:"Server error ",
    invalidInput:"This doesn't look like a lost-item description. Tell us <b>what you lost, where and when</b>. (e.g., I lost a black wallet on Seoul subway line 2 yesterday)",
    bannerCand:"We found strong candidates. <b>Check below</b> if any is yours. (You confirm — we never auto-claim.)",
    bannerOpen:"No exact match yet. We saved your report and will re-check automatically as new items arrive. (See similar candidates below.)",
    extracted:"Extracted", fromPhoto:"Read from photo", searchRegion:"Search area", candQueries:"Search queries",
    candCount:(n)=>`${n} candidate(s) — is any of these yours?`, noCand:"No candidates",
    photoRead:"We read these details from your photo and searched 👇",
    mine:"This is mine", notMine:"Not mine", lblHeld:"Held at", lblRegion:"Region", lblFound:"Found", mapLink:"Map",
    claimDone:"Confirmed! Contact the holding office below to retrieve it.", claimHeldAt:"Held at",
    claimNoOrg:"No office info", claimTelFallback:"· find the contact via the portal below",
    claimHow:"How to retrieve", claimStep1:"Call the holding office", claimStep2:"Visit in person with your passport/ID (identity check)",
    claimStep3:"Check the location on the map", claimTimeline:"Found items are usually held for a limited time (up to ~6 months) before disposal — contact them soon.",
    claimNote:"※ Retrieval follows the office's own identity-check procedure. LostKorea only provides guidance.",
    alertTitle:"Alert settings",
    alertDesc:"Not found yet? We re-check daily as new items arrive and <b>email you when a strong candidate appears</b>. A newly entered email also gets these results now.",
    alertPeriod:"Alert for", days:(n)=>`${n} days`, save:"Save", saving:"Saving…", alertSaved:"Saved.",
    verifySent:(e)=>`We sent a confirmation email to <b>${e}</b>. Press <b>Confirm</b> in that email to turn on alerts.`,
    tooMany:"Too many requests. Please try again shortly.",
    privacyNote:'Please do not include names or phone numbers. Your text/photo is analyzed by Google Gemini for matching (photos are not stored), and reports are deleted automatically 30 days after the alert period ends. By submitting you agree to the <a href="/privacy" target="_blank">Privacy Policy</a> and <a href="/terms" target="_blank">Terms</a>.',
    del:"Delete", delConfirm:"Delete this report? Its text and email are removed immediately and alerts stop.", delFail:"Couldn't delete. Please try again shortly.",
    alertUntil:(d, e)=> e ? `We'll check daily until <b>${d}</b> and alert <b>${e}</b>.`
                          : `We'll re-check daily until <b>${d}</b>. <b>Leave an email</b> to get alerts.`,
    resultsSent:(x)=>`Current results sent to ${x}.`,
    emailPh2:"Email address", emailInvalid:"Enter a valid email.",
    browseXlate:"Item names &amp; descriptions here are in Korean. To read them in your language, use your browser's <b>“Translate page”</b> (right-click → Translate, or the translate icon in the address bar).",
    perPage:"per page", browseCount:(n)=>`${n.toLocaleString()} total`, noItems:"No found items match.", noName:"(no name)",
    prev:"‹ Prev", next:"Next ›", loadFail:"Couldn't load.", mineEmpty:"No reports from this browser yet.",
    mineLoadFail:"Couldn't load the list.", bestMatch:"Best match",
    showMore:(n)=>`Show all (${n})`, collapse:"Collapse",
    stConfirmed:"Confirmed", stCandidate:"Strong match", stOpen:"Searching", stExpired:"Expired",
    watchTo:(d)=>`alerts until ${d}`,
  },
};
// 화면 언어: 서버가 정함(/ → 한국어, /en → 영어. 외국어 브라우저는 /에서 /en으로 이동). 그 외 언어는 번역 위젯.
const LANG = window.LK_LANG === "en" ? "en" : "ko";
const googTarget = () => { const m = document.cookie.match(/(?:^|;\s*)googtrans=\/[^/;]*\/([^;]+)/);
  return m && m[1] ? decodeURIComponent(m[1]) : ""; };
// 서버에 보낼 사용자 언어(판정 근거·알림 메일 언어).
// 1) 번역 위젯에서 고른 언어  2) 입력에 한글이 있으면 ko  3) 영어 화면이면 브라우저 언어(없으면 en)  4) ko
function userLang(text) {
  const g = googTarget();
  if (g && g !== "ko") return g;
  if (text && /[가-힣]/.test(text)) return "ko";
  if (LANG === "en") return (navigator.language || "en").startsWith("ko") ? "en" : (navigator.language || "en");
  return navigator.language || "ko";
}
const t = (k, ...a) => { const v = (I18N[LANG] || I18N.ko)[k]; return typeof v === "function" ? v(...a) : (v ?? k); };
const mapQuery = (m) => m.org_name || [m.region, m.dep_place].filter(Boolean).join(" ") || m.dep_place || "";
// 한국에선 네이버 지도가 길찾기·대중교통까지 정확(영어 화면 지원). 구글 지도는 외국인에게 익숙해서 함께.
const naverUrl = (m) => "https://map.naver.com/p/search/" + encodeURIComponent(mapQuery(m));
const mapUrl = (m) => "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(mapQuery(m));
const mapLinks = (m) => (m.org_name || m.region)
  ? `<a href="${naverUrl(m)}" target="_blank" rel="noopener" class="maplink">${ic('map', 'ic ic-sm')}${t('naverMap')}</a>
     <a href="${mapUrl(m)}" target="_blank" rel="noopener" class="maplink">${ic('map', 'ic ic-sm')}${t('googleMap')}</a>` : "";
// EN 모드: grade가 함께 만든 영어 물품명·설명 사용(없으면 한국어 원문). 둘러보기 항목은 번역 없음.
const dispName = (m) => (LANG === "en" && m.name_en) ? m.name_en : m.name;
const dispDesc = (m) => (LANG === "en" && m.desc_en) ? m.desc_en : cleanDesc(m.description);

const EXAMPLES = {
  ko: [["검정 지갑", "어제 서울 지하철 2호선에서 검정색 지갑을 잃어버렸어요"],
       ["에어팟", "강남역 근처에서 흰색 에어팟을 잃어버렸어요"],
       ["3단 우산", "부산 지하철에서 3단 우산을 두고 내렸어요"],
       ["카드지갑", "명동에서 카드지갑을 잃어버렸어요. 신분증이랑 카드가 들어있어요"]],
  en: [["Passport", "I lost my passport in a black cover near Myeongdong yesterday"],
       ["Wallet", "I left a brown leather wallet on the subway near Hongdae station"],
       ["AirPods", "I lost white AirPods Pro around Gangnam station"],
       ["Phone", "I lost my iPhone in a blue case in a taxi from Incheon Airport to Seoul"]],
};

function applyLang() {
  window.xlateFailText = t('xlateFail');
  const ex = $("examples");
  if (ex) {
    ex.innerHTML = `<span class="muted">${t('exLabel')}</span>` +
      EXAMPLES[LANG].map(([label, q], i) => `<button type="button" class="ex" data-ex="${i}">${esc(label)}</button>`).join("");
    ex.onclick = (e) => { const b = e.target.closest(".ex"); if (b) tryExample(EXAMPLES[LANG][+b.dataset.ex][1]); };
  }
  const g = googTarget();
  if (g && g !== LANG) {                 // 전에 고른 언어(쿠키) → 선택 표시 + 다시 번역
    if ($("langPicker")) $("langPicker").value = g;
    translateTo(g).catch(() => {});
  }
}

function clearGoogTrans() {
  const host = location.hostname;
  for (const d of ["", "; domain=" + host, "; domain=." + host]) {
    document.cookie = "googtrans=; path=/; expires=Thu, 01 Jan 1970 00:00:00 UTC" + d;
  }
}

// 번역 위젯은 한/영 외 언어를 고를 때만 비동기 로딩. 막힌 네트워크면 8초 뒤 안내하고 원래 언어 유지.
let _xlate = null;
function loadTranslate() {
  if (_xlate) return _xlate;
  _xlate = new Promise((resolve, reject) => {
    window.googleTranslateElementInit = () => {
      new google.translate.TranslateElement(
        { pageLanguage: LANG, includedLanguages: 'en,ko,ja,zh-CN,zh-TW,vi,th,id,ms,hi,fr,es,ru,de', autoDisplay: false },
        'google_translate_element');
      resolve();
    };
    const sc = document.createElement("script");
    sc.src = "https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit";
    sc.async = true; sc.onerror = reject;
    document.head.appendChild(sc);
    setTimeout(() => reject(new Error("timeout")), 8000);
  });
  return _xlate;
}

// 위젯을 불러온 직후엔 언어를 골라도 번역이 안 걸리는 경우가 많음(실측: 첫 선택 무시, 두 번째에 적용.
// html의 translated-ltr 클래스는 붙어도 글자는 그대로인 경우가 있어 신호로 못 씀)
// → 제목 글자가 실제로 바뀔 때까지 1.5초마다 다시 선택. 최대 ~20초.
function translateTo(code) {
  const h1 = document.querySelector(window.LK_VIEW === "browse" ? ".page-head h1" : ".hero h1");
  const orig = h1 ? h1.textContent : "";
  return loadTranslate().then(() => new Promise(resolve => {
    let tries = 0;
    (function set() {
      const combo = document.querySelector(".goog-te-combo");
      if (h1 && h1.textContent !== orig) return resolve();
      if (combo && combo.querySelector(`option[value="${code}"]`) && tries % 3 === 0) {
        combo.value = code; combo.dispatchEvent(new Event("change"));
      }
      if (tries++ < 40) setTimeout(set, 500); else resolve();
    })();
  }));
}

function chips(items, cls="") {
  return `<div class="chips">${items.filter(Boolean).map(x=>`<span class="chip ${cls}">${esc(x)}</span>`).join("")}</div>`;
}

const validImg = (u) => (u && u.startsWith('http') && !u.includes('no_img')) ? u : '';
const cleanDesc = (s) => (s||'').replace(/^\s*내용\s*/,'').replace(/\s+/g,' ').trim();

function openLightbox(url){ $("lightboxImg").src = url; $("lightbox").hidden = false; }
function closeLightbox(){ $("lightbox").hidden = true; $("lightboxImg").src = ""; }
document.addEventListener('click', e => { const t = e.target.closest('.thumb'); if (t && t.dataset.full) openLightbox(t.dataset.full); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

// preview=true: 진행 중(아직 저장 전 — 신고 id 없음) 후보 미리보기 → 확인/아니에요 버튼 숨김
function matchCard(m, idx, preview = false) {
  const g = m.grade;
  const img = validImg(m.image_url);
  const thumb = img
    ? `<img class="thumb" data-full="${esc(img)}" src="${esc(img)}" alt="" referrerpolicy="no-referrer" onerror="this.style.display='none'"
         title="${t('zoom')}"
         style="width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid var(--line);flex:0 0 56px;cursor:zoom-in" />`
    : '';
  const btns = preview ? '' : `<div style="margin-top:8px; display:flex; gap:6px; flex-wrap:wrap">
      <button onclick="confirmMatch(${idx})" class="btn btn-success btn-xs">${ic('check', 'ic ic-sm')}${t('mine')}</button>
      <button onclick="dismissMatch(${idx})" class="btn btn-outline btn-xs">${t('notMine')}</button>
      ${mapLinks(m)}
    </div>`;
  return `<div class="match" data-idx="${idx}">
    <div class="grade" style="background:${gradeColor(g)}" title="${esc(t('scoreLegend'))}">${g==null?'–':g}<small>${t('scoreLabel')}</small></div>
    ${thumb}
    <div class="m-body">
      <div class="m-title">${esc(dispName(m))} <span class="muted">/ ${esc(m.color)}</span></div>
      <div class="m-meta">${m.category ? esc(m.category) + " · " : ""}${t('lblHeld')}: ${esc(m.dep_place)||'-'} · ${t('lblRegion')}: ${esc(m.region)||'-'} · ${t('lblFound')}: ${esc(m.found_at)||'-'}</div>
      ${(m.org_name||m.tel) ? `<div class="m-meta"><span>${ic('building')}${esc(m.org_name)||'-'}</span>${m.tel?`<span>${ic('phone')}<a href="tel:${esc(m.tel)}">${esc(m.tel)}</a></span>`:''}</div>` : ''}
      ${dispDesc(m) ? `<div class="m-reason">${ic('note')}<span>${esc(dispDesc(m).slice(0,160))}${dispDesc(m).length>160?'…':''}</span></div>` : ''}
      ${m.reason ? `<div class="m-reason">${ic('spark')}<span>${esc(m.reason)}</span></div>` : ''}
      ${m.visual_score != null ? `<div class="m-reason" style="color:${gradeColor(m.visual_score)}">${ic('camera')}<span><b>${t('visualSim')} ${m.visual_score}</b>${m.visual_reason ? ` <span class="muted">— ${esc(m.visual_reason)}</span>` : ''}</span></div>` : ''}
      ${btns}
    </div>
  </div>`;
}

let _lid = null, _token = "", _matches = [];

// 등록 응답의 (id, 관리 토큰)을 이 브라우저에 보관 → '내 신고'·확인·제외·알림 설정에 사용(서버엔 해시만 있음)
const MINE_KEY = "lostkorea_mine";
// 이름 변경 전(FindIt) 키에 보관된 '내 신고'를 새 키로 옮김 — 기존 사용자의 신고 관리 토큰을 잃지 않게
try {
  const old = localStorage.getItem("findit_mine");
  if (old && !localStorage.getItem(MINE_KEY)) localStorage.setItem(MINE_KEY, old);
  if (old) localStorage.removeItem("findit_mine");
} catch (e) {}
function mineRefs() { try { return JSON.parse(localStorage.getItem(MINE_KEY) || "[]"); } catch (e) { return []; } }
function saveMineRef(id, token) {
  if (!id || !token) return;
  try { localStorage.setItem(MINE_KEY, JSON.stringify([{ id, token }, ...mineRefs().filter(r => r.id !== id)].slice(0, 50))); } catch (e) {}
}
const authHeaders = () => ({ "Content-Type": "application/json", "X-Report-Token": _token });
async function errText(res) {  // 서버 오류 → 사람이 읽을 문구(429는 이용량 안내)
  try { const d = await res.json(); if (d && typeof d.detail === "string") return d.detail; } catch (e) {}
  return res.status === 429 ? t('tooMany') : t('serverErr') + res.status;
}

async function confirmMatch(idx) {
  const m = _matches[idx];
  try {
    await fetch(`/lost-items/${_lid}/confirm`, { method:"POST", headers: authHeaders(), body: JSON.stringify({ atc_id: m.atc_id }) });
  } catch (e) {}
  $("result").innerHTML = claimGuide(m);
  loadMine();
}

async function dismissMatch(idx) {
  const m = _matches[idx];
  try {
    await fetch(`/lost-items/${_lid}/dismiss`, { method:"POST", headers: authHeaders(), body: JSON.stringify({ atc_id: m.atc_id }) });
  } catch (e) {}
  const card = document.querySelector(`#result .match[data-idx="${idx}"]`);
  if (card) card.remove();
  loadMine();
}

function claimGuide(m) {
  const img = validImg(m.image_url);
  const thumb = img ? `<img class="thumb" data-full="${esc(img)}" src="${esc(img)}" style="width:72px;height:72px;object-fit:cover;border-radius:8px;flex:0 0 72px;cursor:zoom-in" />` : '';
  return `<div class="banner matched">${ic('check')}<span>${t('claimDone')}</span></div>
    <div class="match">
      ${thumb}
      <div class="m-body">
        <div class="m-title">${esc(dispName(m))} <span class="muted">/ ${esc(m.color)}</span></div>
        <div class="m-meta">${t('claimHeldAt')}: ${esc(m.dep_place)||'-'} · ${t('lblRegion')}: ${esc(m.region)||'-'} · ${t('lblFound')}: ${esc(m.found_at)||'-'}</div>
        <div style="margin-top:8px; font-size:14px">
          ${ic('building')} <b>${esc(m.org_name)||t('claimNoOrg')}</b>
          ${m.tel ? `· ${ic('phone')} <a href="tel:${esc(m.tel)}">${esc(m.tel)}</a>` : '· '+t('claimTelFallback').replace(/^· /,'')}
        </div>
        <ol style="margin:10px 0 0; padding-left:20px; font-size:13px; line-height:1.7">
          <li><b>${t('claimStep1')}</b>${m.tel?` — <a href="tel:${esc(m.tel)}">${esc(m.tel)}</a>`:''}</li>
          <li>${t('claimStep2')}</li>
          <li>${t('claimStep3')} — <a href="${naverUrl(m)}" target="_blank" rel="noopener">${t('naverMap')}</a> ·
              <a href="${mapUrl(m)}" target="_blank" rel="noopener">${t('googleMap')}</a></li>
        </ol>
        <div class="note info" style="margin-top:10px">${ic('speech', 'ic ic-sm')} ${t('helpline')}</div>
        <div style="margin-top:8px; font-size:13px">${ic('link', 'ic ic-sm')} <a href="https://www.lost112.go.kr" target="_blank" rel="noopener">lost112.go.kr</a> ${t('lost112')}</div>
      </div>
    </div>
    <p class="muted small" style="margin-top:10px">${ic('clock', 'ic ic-sm')} ${t('claimTimeline')}</p>
    <p class="muted" style="font-size:13px">${t('claimNote')}</p>`;
}

function renderResult(d, userPhoto, timing = "") {
  if (d.status === "invalid") {  // 분실물 설명이 아님 → 검색 안 함
    _lid = null; _matches = [];
    $("result").innerHTML = `<div class="banner open">${ic('info')}<span>${t('invalidInput')}</span></div>`;
    $("resultCard").hidden = false;
    $("resultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  _lid = d.id; _token = d.token || ""; _matches = d.matches || [];
  saveMineRef(d.id, d.token);
  const ex = d.extracted || {};
  const exChips = [ex.item, ex.brand, ex.color, ex.category && (t('catPrefix') + ex.category), ex.place_context, ex.lost_date, ex.features];
  const banner = d.status === "candidate"
    ? `<div class="banner candidate">${ic('bell')}<span>${t('bannerCand')}</span></div>`
    : `<div class="banner open">${ic('clock')}<span>${t('bannerOpen')}</span></div>`;
  const photoBlock = userPhoto
    ? `<div style="margin-bottom:10px; display:flex; gap:10px; align-items:center">
         <img src="${userPhoto}" style="width:72px;height:72px;object-fit:cover;border-radius:10px;border:1px solid var(--line)" />
         <div class="muted" style="font-size:12px">${t('photoRead')}</div>
       </div>` : "";
  const cards = (d.matches||[]).slice(0,6).map((m,i)=>matchCard(m,i)).join("");
  $("result").innerHTML =
    `${banner}${photoBlock}
     <div class="cand-head"><b>${t('candCount', d.count)}</b>${cards ? `<span class="muted legend">${t('scoreLegend')}</span>` : ""}</div>
     ${cards || `<span class="muted">${t('noCand')}</span>`}
     ${alertPanel(d)}
     <details class="how-searched"><summary>${t('howSearched')}</summary>
       <div><b>${userPhoto ? t('fromPhoto') : t('extracted')}</b> ${chips(exChips)}</div>
       ${d.region_set?.length ? `<div>${regionExplain(ex)}<b>${t('searchRegion')}</b> ${chips(d.region_set, "k")}</div>` : ""}
       <div><b>${t('candQueries')}</b> ${chips(d.queries)}</div>
       ${timing}
     </details>`;
  $("resultCard").hidden = false;
  $("resultCard").scrollIntoView({ behavior:"smooth", block:"nearest" });
  loadMine();
}

function regionExplain(ex) {
  const place = (ex && ex.place_context || "").trim();
  let msg;
  if (LANG === "en") {
    const where = place ? `You lost it around <b>${esc(place)}</b>. ` : "";
    msg = `${where}In Korea, lost items often <b>travel along the route</b> (a subway or bus line) and turn up in other districts.
      <b>You don't need to know the line number or direction</b> — a station, area or landmark is enough. We searched
      <b>every district the route passes through</b> (below) and <b>boosted</b> them in ranking (we don't filter — nothing gets excluded).`;
  } else {
    const where = place ? `<b>${esc(place)}</b>에서 잃어버리셨네요. ` : "";
    msg = `${where}분실물은 <b>이동 경로(노선·교통편)를 따라</b> 다른 지역에서 발견되기도 해요.
      그래서 아래 지역들까지 <b>넓게</b> 살펴 순위에 반영했어요. (좁히지 않고 가점만 — 놓치지 않으려고요)`;
  }
  return `<div class="note info" style="margin-bottom:8px">${ic('pin', 'ic ic-sm')} ${msg}</div>`;
}

const WATCH_DAYS = [7, 30, 90, 180];  // 알림 기간 선택지(180 = 습득물 최대 보관 ~6개월)

// 결과창 '알림 설정' — 등록 때 이메일을 넣었든 안 넣었든 항상 표시. 이메일·약한 후보·기간을 여기서 변경.
function alertPanel(d) {
  const email = "";
  const opts = WATCH_DAYS.map(n => `<option value="${n}" ${n === 30 ? "selected" : ""}>${t('days', n)}</option>`).join("");
  return `<div id="alertPanel" class="alert-panel">
      <div class="alert-title">${ic('bell')}${t('alertTitle')}</div>
      <div class="muted small" style="margin-top:4px">${t('alertDesc')}</div>
      <div class="row">
        <input id="alertEmail" type="email" class="input" value="${esc(email)}" placeholder="${t('emailPh2')}" />
        <label class="date-lbl">${t('alertPeriod')} <select id="alertDays" class="input">${opts}</select></label>
        <button class="btn btn-primary" onclick="saveAlerts()">${t('save')}</button>
      </div>
      <label style="display:flex; align-items:center; gap:6px; margin-top:8px; font-size:13px; color:var(--sub); cursor:pointer">
        <input type="checkbox" id="alertWeak" />
        <span>${t('notifyWeak')}</span>
      </label>
      <div id="alertMsg" style="font-size:13px; margin-top:8px">${d.email_pending ? t('verifySent', esc(email)) : (d.watch_until ? t('alertUntil', d.watch_until.slice(0, 10), "") : "")}</div>
    </div>`;
}

async function saveAlerts() {
  const email = ($("alertEmail").value || "").trim();
  if (email && !email.includes("@")) { $("alertMsg").textContent = t('emailInvalid'); return; }
  $("alertMsg").textContent = t('saving');
  try {
    const res = await fetch(`/lost-items/${_lid}/alerts`, {
      method:"POST", headers: authHeaders(),
      body: JSON.stringify({ email, notify_weak: $("alertWeak").checked, watch_days: +$("alertDays").value }) });
    if (!res.ok) throw new Error(await errText(res));
    const r = await res.json();
    $("alertWeak").checked = r.notify_weak;  // 이메일 없으면 서버가 옵트인 무효화 → 화면도 맞춤
    $("alertMsg").innerHTML = `${ic('check', 'ic ic-sm')} ${t('alertSaved')} ` + (r.verification_sent ? t('verifySent', esc(r.email))
      : t('alertUntil', r.watch_until.slice(0, 10), r.email_verified ? esc(r.email) : ""));
    loadMine();
  } catch (e) {
    $("alertMsg").textContent = t('errPrefix') + e.message;
  }
}

// ── 실시간 진행: 서버가 단계마다 보내는 SSE(event: 단계명, data: JSON)를 읽어 패널을 갱신 ──
async function streamPost(url, body, onEvent) {
  const res = await fetch(url, { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new Error(await errText(res));
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {           // 이벤트 경계 = 빈 줄
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      let ev = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(ev, JSON.parse(data));
    }
  }
}

let _prog = null;
const stepLabel = (s) => t(s === "extract" && _prog?.isImg ? "step_extractImg" : "step_" + s);

function progStart(isImg) {
  const steps = ["extract", "route", "fanout", "search", "grade"].concat(isImg ? ["visual"] : []);
  const now = performance.now();
  _prog = { isImg, steps, done: {}, info: {}, times: {}, cands: [], t0: now, last: now };
  renderProgress();
  $("resultCard").hidden = false;
  $("resultCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function stepSummary(ev, d) {
  if (ev === "extract") {
    const ex = d.extracted || {};
    return chips([ex.item, ex.brand, ex.color, ex.category && (t('catPrefix') + ex.category), ex.place_context, ex.lost_date, ex.features]);
  }
  if (ev === "route") {
    const r = d.region_set || [];
    return r.length ? `${esc(t('regionCount', r.length))}${chips(r.slice(0, 10).concat(r.length > 10 ? ["…"] : []), "k")}`
                    : `<span class="muted">${t('noRegion')}</span>`;
  }
  if (ev === "fanout") return chips(d.queries || []);
  if (ev === "search") return esc(t('searchCount', (d.candidates || []).length));
  if (ev === "grade") return esc(t('gradeDone', (d.matches || [])[0]?.grade ?? "-"));
  if (ev === "visual") return esc(t('visualDone', (d.matches || []).filter(m => m.visual_score != null).length));
  return "";
}

function progStep(ev, d) {
  if (!_prog || !_prog.steps.includes(ev)) return;
  const now = performance.now();
  _prog.times[ev] = (now - _prog.last) / 1000; _prog.last = now;
  _prog.done[ev] = true;
  _prog.info[ev] = stepSummary(ev, d);
  $("srStatus").textContent = `${stepLabel(ev)} ✓`;
  if (ev === "search") _prog.cands = d.candidates || [];          // 판정 전 후보 먼저 표시
  if (ev === "grade" || ev === "visual") _prog.cands = d.matches || [];
  renderProgress();
}

function renderProgress() {
  const p = _prog, cur = p.steps.find(s => !p.done[s]);
  const rows = p.steps.map(s => {
    const st = p.done[s] ? "done" : s === cur ? "run" : "wait";
    const icon = st === "done" ? `<span class="dot">${ic('check')}</span>` : st === "run" ? '<span class="spin"></span>' : '<span class="dot"></span>';
    const info = p.done[s] ? p.info[s] : st === "run" ? `<span class="muted">${t('stepWorking')}</span>` : "";
    return `<div class="prog-row ${st}"><span>${icon}</span><b>${stepLabel(s)}</b>
      <div class="prog-info">${info}</div></div>`;
  }).join("");
  const cards = p.cands.slice(0, 6).map((m, i) => matchCard(m, i, true)).join("");
  $("result").innerHTML = `<div class="prog">${rows}</div>${cards}`;
}

function progTiming() {
  const p = _prog; if (!p) return "";
  const parts = p.steps.filter(s => p.times[s] != null).map(s => `${stepLabel(s)} ${p.times[s].toFixed(1)}s`);
  const total = ((performance.now() - p.t0) / 1000).toFixed(1);
  return `<div class="muted">${ic('clock', 'ic ic-sm')} ${t('total')} ${total}s (${esc(parts.join(" · "))})</div>`;
}

// 단계 스트림 실행 → 진행 패널 갱신 → 최종 결과(result 이벤트)로 기존 결과 화면 렌더
async function runMatchStream(url, body, userPhoto) {
  progStart(!!userPhoto);
  let final = null;
  await streamPost(url, body, (ev, d) => {
    if (ev === "result") final = d;
    else if (ev === "error") throw new Error(d.detail);
    else progStep(ev, d);
  });
  if (!final) throw new Error(t('streamFail'));
  renderResult(final, userPhoto, final.status === "invalid" ? "" : progTiming());
}

// Enter = 검색, Shift+Enter = 줄바꿈. 한글 조합 중 Enter(isComposing)는 무시 — 안 그러면 마지막 글자가 깨지거나 두 번 검색
function onTextKey(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); search(); }
}

function search() {
  if ($("go").disabled) return;
  return _photoB64 ? registerImage() : registerLost();
}

async function registerLost() {
  const text = $("text").value.trim();
  if (!text) { $("hint").textContent = t('needText'); $("text").focus(); return; }
  const btn = $("go"); btn.disabled = true;
  const orig = btn.textContent; btn.innerHTML = '<span class="spin"></span>' + t('searching');
  $("hint").textContent = "";
  try {
    const body = { text, lost_date: $("lostDate").value || null, lang: userLang(text) };
    await runMatchStream("/lost-items/stream", body);
  } catch (e) {
    $("hint").textContent = t('errPrefix') + e.message;
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

let _photoB64 = null, _photoMime = null, _photoDataUrl = null;

const PHOTO_MAX_PX = 1600;
// 업로드 전 축소(긴 변 1600px JPEG) — Vision 비용·전송량·서버 메모리 절감. 못 그리는 형식(HEIC 등)은 원본.
function shrinkPhoto(dataUrl) {
  return new Promise(resolve => {
    const im = new Image();
    im.onload = () => {
      const k = Math.min(1, PHOTO_MAX_PX / Math.max(im.width, im.height));
      const c = document.createElement("canvas");
      c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
      resolve(c.toDataURL("image/jpeg", 0.85));
    };
    im.onerror = () => resolve(null);
    im.src = dataUrl;
  });
}

function onPhoto(e) {
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    const small = await shrinkPhoto(rd.result);
    _photoDataUrl = small || rd.result;
    _photoMime = small ? "image/jpeg" : (f.type || "image/jpeg");
    _photoB64 = _photoDataUrl.split(",")[1];
    $("photoPreview").src = _photoDataUrl;
    $("photoChip").hidden = false;
    $("hint").textContent = "";
  };
  rd.readAsDataURL(f);
}

function clearPhoto() {
  _photoB64 = _photoMime = _photoDataUrl = null;
  $("photo").value = ""; $("photoChip").hidden = true; $("photoPreview").src = "";
}

async function registerImage() {
  if (!_photoB64) { $("hint").textContent = t('needPhoto'); return; }
  const btn = $("go"); btn.disabled = true;
  const orig = btn.textContent; btn.innerHTML = '<span class="spin"></span>' + t('searching');
  $("hint").textContent = "";
  try {
    const body = { image_b64: _photoB64, mime: _photoMime,
                   note: $("text").value.trim(), lost_date: $("lostDate").value || null,
                   lang: userLang($("text").value.trim()) };
    await runMatchStream("/lost-items/image/stream", body, _photoDataUrl);
  } catch (e) {
    $("hint").textContent = t('errPrefix') + e.message;
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

const MINE_LIMIT = 5;                 // 최근 N개만 기본 표시(길어지지 않게)
let _mineItems = [], _mineExpanded = false;

async function loadMine() {
  const refs = mineRefs();
  if (!refs.length) { _mineItems = []; renderMine(); return; }   // 이 브라우저에 신고가 없으면 서버 호출도 생략
  $("mine").innerHTML = `<span class="muted">${t('loading')}</span>`;  // 새로고침 피드백
  try {
    _mineItems = await (await fetch("/lost-items/mine", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: refs }) })).json();
    renderMine();
  } catch (e) {
    $("mine").innerHTML = `<span class="muted">${t('mineLoadFail')}</span>`;
  }
}

function mineItem(it) {
  const bm = it.best_match;
  const when = (it.created_at || "").slice(0, 10);
  const stLabel = it.status === "confirmed" ? t("stConfirmed")
    : (it.status === "candidate" || it.status === "matched") ? t("stCandidate")
    : it.status === "expired" ? t("stExpired") : t("stOpen");
  const watch = it.status === "open" && it.watch_until ? " · " + t("watchTo", it.watch_until.slice(0, 10)) : "";
  return `<div class="mine-item">
    <div style="display:flex; justify-content:space-between; gap:10px">
      <div style="min-width:0">
        <div>${esc(it.text)}</div>
        <div class="m-meta">${when}${bm ? " · " + t("bestMatch") + ": " + esc(bm.name) + " (grade " + (it.best_grade ?? "-") + ")" : ""}${watch}</div>
      </div>
      <div style="text-align:right; flex:0 0 auto">
        <span class="status ${it.status === "matched" ? "candidate" : it.status}">${stLabel}</span>
        <div><button class="mine-del" onclick="deleteMine('${esc(it.id)}')">${t('del')}</button></div>
      </div>
    </div>
  </div>`;
}

// 신고 삭제(신고자 요청 → 서버에서 즉시 완전 삭제) + 이 브라우저의 보관 토큰도 제거
async function deleteMine(id) {
  if (!confirm(t('delConfirm'))) return;
  const ref = mineRefs().find(r => r.id === id);
  try {
    const res = await fetch(`/lost-items/${id}`, { method: "DELETE",
      headers: { "X-Report-Token": ref ? ref.token : "" } });
    if (!res.ok && res.status !== 404) { alert(await errText(res)); return; }
  } catch (e) { alert(t('delFail')); return; }
  try { localStorage.setItem(MINE_KEY, JSON.stringify(mineRefs().filter(r => r.id !== id))); } catch (e) {}
  if (_lid === id) $("result").innerHTML = "";
  loadMine();
}

function renderMine() {
  const items = _mineItems;
  $("mineCard").hidden = !items.length;
  if (!items.length) { $("mine").innerHTML = `<span class="muted">${t('mineEmpty')}</span>`; return; }
  const shown = _mineExpanded ? items : items.slice(0, MINE_LIMIT);
  let html = shown.map(mineItem).join("");
  if (items.length > MINE_LIMIT) {
    html += `<button class="ghost" onclick="toggleMine()" style="width:100%; margin-top:10px">
      ${_mineExpanded ? t('collapse') : t('showMore', items.length)}</button>`;
  }
  $("mine").innerHTML = html;
}

function toggleMine() { _mineExpanded = !_mineExpanded; renderMine(); }
function startSearch() {
  const el = $("text");
  if (!el || window.LK_VIEW === "browse") { location.href = (LANG === "en" ? "/en" : "/") + "#search"; return; }
  document.getElementById("search").scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => el.focus({ preventScroll: true }), 400);
}

function tryExample(text) {
  clearPhoto();
  $("text").value = text; $("textCount").textContent = text.length;
  registerLost();  // 바로 검색
}

// 데이터 규모(홈 통계 띠) — 색인 문서 수·최신 습득일
async function fetchStats() {
  if (!$("statTotal")) return;
  try {
    const d = await (await fetch("/found-items/browse?size=1")).json();
    $("statTotal").textContent = (d.total || 0).toLocaleString(LANG === "en" ? "en-US" : "ko-KR");
    $("statLatest").textContent = d.items?.[0]?.found_at || "—";
  } catch (e) { /* 통계가 없어도 페이지는 그대로 */ }
}

applyLang();
if (window.LK_VIEW === "browse") doBrowse(1);
else { loadMine(); fetchStats(); }

async function doBrowse(page) {
  const p = new URLSearchParams({
    q: $("bq").value.trim(), region: $("bregion").value,
    date_from: $("bfrom").value, date_to: $("bto").value, page, size: $("bsize").value || 10,
  });
  $("browseList").innerHTML = `<span class="muted">${t('loading')}</span>`;
  try {
    const d = await (await fetch("/found-items/browse?" + p)).json();
    renderBrowse(d);
  } catch (e) {
    $("browseList").innerHTML = `<span class="muted">${t('loadFail')}</span>`;
  }
}

function resetBrowse() {
  $("bq").value = ""; $("bregion").value = ""; $("bfrom").value = ""; $("bto").value = "";
  doBrowse(1);
}

function renderBrowse(d) {
  const sel = $("bregion");
  if (d.regions?.length && sel.dataset.filled !== "1") {  // 지역 드롭다운 최초 1회 채움
    const cur = sel.value;
    sel.innerHTML = `<option value="">${t('allRegions')}</option>` +
      d.regions.slice().sort((a, b) => b.count - a.count)
        .map(r => `<option value="${esc(r.name)}">${esc(r.name)} (${r.count})</option>`).join("");
    sel.value = cur; sel.dataset.filled = "1";
  }
  $("bcount").textContent = t('browseCount', d.total || 0);
  const xl = t('browseXlate');
  $("browseList").innerHTML = (xl ? `<div class="note info" style="margin-bottom:8px">${ic('info', 'ic ic-sm')} ${xl}</div>` : "") +
    ((d.items || []).map(browseItem).join("") || `<span class="muted">${t('noItems')}</span>`);
  renderPager(d.page || 1, Math.ceil((d.total || 0) / (d.size || 24)));
}

function browseItem(m) {
  const img = validImg(m.image_url);
  const thumb = img
    ? `<img class="thumb" data-full="${esc(img)}" src="${esc(img)}" referrerpolicy="no-referrer" onerror="this.style.display='none'"
         title="${t('zoom')}"
         style="width:56px;height:56px;object-fit:cover;border-radius:8px;border:1px solid var(--line);flex:0 0 56px;cursor:zoom-in" />`
    : '';
  return `<div class="bitem">${thumb}
    <div class="m-body">
      <div class="m-title">${esc(m.name) || t('noName')} <span class="muted">/ ${esc(m.color) || "-"}</span></div>
      <div class="m-meta">${esc(m.category) || "-"} · ${t('lblRegion')}: ${esc(m.region) || "-"} · ${t('lblFound')}: ${esc(m.found_at) || "-"}</div>
      ${(m.org_name || m.tel) ? `<div class="m-meta"><span>${ic('building')}${esc(m.org_name) || "-"}</span>${m.tel ? `<span>${ic('phone')}<a href="tel:${esc(m.tel)}">${esc(m.tel)}</a></span>` : ""}${(m.org_name||m.region)?`<span>${ic('map')}<a href="${naverUrl(m)}" target="_blank" rel="noopener">${t('naverMap')}</a> · <a href="${mapUrl(m)}" target="_blank" rel="noopener">${t('googleMap')}</a></span>`:""}</div>` : ""}
      ${cleanDesc(m.description) ? `<div class="m-reason">${ic('note')}<span>${esc(cleanDesc(m.description).slice(0, 120))}${cleanDesc(m.description).length > 120 ? "…" : ""}</span></div>` : ""}
    </div></div>`;
}

function renderPager(page, pages) {
  if (!pages || pages <= 1) { $("browsePager").innerHTML = ""; return; }
  const btn = (p, label, dis) => `<button class="btn btn-outline btn-sm" ${dis ? "disabled" : ""} onclick="doBrowse(${p})">${label}</button>`;
  $("browsePager").innerHTML = btn(page - 1, t('prev'), page <= 1)
    + `<span class="muted small">${page} / ${pages}</span>`
    + btn(page + 1, t('next'), page >= pages);
}
