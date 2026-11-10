// Preserve the mobile navigation strip's horizontal position across page loads.
(() => {
  const nav = document.querySelector(".nav");
  const current = Array.from(nav.querySelectorAll("a")).find(link => link.pathname === location.pathname);
  if (current) current.setAttribute("aria-current", "page");
  try {
    nav.scrollLeft = Number(sessionStorage.getItem("evedm-nav-scroll")) || 0;
    window.addEventListener("pagehide", () => sessionStorage.setItem("evedm-nav-scroll", String(nav.scrollLeft)));
  } catch { /* Navigation also works when browser storage is unavailable. */ }
})();
