// Runs first in <head>: applies the theme the reader picked (light, dark or the system's) before the
// page is drawn, and wires the switch in the top bar. Stored in this browser only.
(() => {
  const root = document.documentElement;
  const read = () => { try { return localStorage.getItem("as.theme") || "system"; } catch { return "system"; } };
  const apply = (t) => { if (t === "light" || t === "dark") root.dataset.theme = t; else delete root.dataset.theme; };
  apply(read());
  document.addEventListener("DOMContentLoaded", () => {
    const buttons = document.querySelectorAll("[data-theme-set]");
    const mark = (t) => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
    mark(read());
    buttons.forEach((b) => b.addEventListener("click", () => {
      const t = b.dataset.themeSet;
      try { localStorage.setItem("as.theme", t); } catch { /* private mode: this page only */ }
      apply(t);
      mark(t);
    }));
  });
})();
