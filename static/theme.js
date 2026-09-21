// Runs before first paint so the page never flashes the wrong theme.
// Uses the same "theme" localStorage key as earlier versions.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the OS setting */ }
})();
