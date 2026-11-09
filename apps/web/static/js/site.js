// 모든 페이지 공통 — 모바일 메뉴 토글, 언어 선택(한국어·영어는 해당 페이지로, 그 외 언어는 번역 위젯).
(function () {
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("siteNav");
  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    nav.addEventListener("click", (e) => {            // 메뉴에서 같은 페이지 앵커로 이동하면 닫기
      if (e.target.closest("a")) { nav.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); }
    });
  }

  const sel = document.getElementById("langPicker");
  if (!sel) return;
  const lang = document.documentElement.lang || "ko";
  sel.addEventListener("change", () => {
    const code = sel.value;
    if (code === "ko" || code === "en") {
      // 번역 위젯 쿠키를 지우고 해당 언어 페이지로
      const host = location.hostname;
      for (const d of ["", "; domain=" + host, "; domain=." + host]) {
        document.cookie = "googtrans=; path=/; expires=Thu, 01 Jan 1970 00:00:00 UTC" + d;
      }
      const target = sel.dataset[code] || (code === "en" ? "/en" : "/");
      if (code !== lang || location.pathname !== target) location.href = target + location.hash;
      return;
    }
    if (typeof window.translateTo === "function") {   // 홈·전체 습득물: 번역 위젯
      window.translateTo(code).catch(() => { sel.value = lang; alert(window.xlateFailText || "Translation unavailable."); });
    } else {                                           // 문서 페이지: 영어 화면으로
      location.href = sel.dataset.en || "/en";
    }
  });
})();
