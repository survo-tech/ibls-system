/* Sets the saved theme before first paint (loaded synchronously in <head>). No tracking. */
(function () {
  var r = document.documentElement;
  r.classList.add("js");
  try {
    var t = localStorage.getItem("ibls-theme");
    if (t === "light" || t === "dark") r.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked */ }
})();
