// Applies the saved light / dark choice before first paint (falls back to the OS setting).
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the OS setting */ }
})();
