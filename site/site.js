/* India Business Leaders Survey: small page behaviours (section nav, theme toggle). No tracking. */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  root.classList.add("js");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // Build the top section nav and the footer nav from <section data-nav="...">.
  function autoNav() {
    var sections = [].slice.call(doc.querySelectorAll("section[data-nav]"));
    var links = sections.map(function (s) {
      return '<a href="#' + esc(s.id) + '">' + esc(s.getAttribute("data-nav")) + "</a>";
    }).join("");
    var header = doc.querySelector("header.nav[data-auto]");
    if (header) {
      header.innerHTML = '<nav class="seg" aria-label="Sections">' + links + "</nav>" +
        '<button class="theme-btn" id="theme-btn" type="button" aria-label="Switch to dark theme"></button>';
    }
    [].slice.call(doc.querySelectorAll(".foot nav[data-auto]")).forEach(function (n) {
      n.innerHTML = '<a href="#top">Top</a>' + links;
    });
  }

  // Light/dark toggle, remembered in localStorage when available.
  function theme() {
    var btn = doc.getElementById("theme-btn"), mq = window.matchMedia("(prefers-color-scheme: dark)");
    try { var saved = localStorage.getItem("ibls-theme"); if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved); } catch (e) { /* storage blocked */ }
    function current() { var t = root.getAttribute("data-theme"); return t === "light" || t === "dark" ? t : (mq.matches ? "dark" : "light"); }
    function label() { if (btn) btn.setAttribute("aria-label", current() === "dark" ? "Switch to light theme" : "Switch to dark theme"); }
    if (btn) btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("ibls-theme", next); } catch (e) { /* storage blocked */ }
      label();
    });
    label();
  }

  // Highlight the nav item for the section currently in view.
  function scrollSpy() {
    var seg = doc.querySelector(".seg");
    if (!seg) return;
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var links = [].slice.call(seg.querySelectorAll("a"));
    var targets = links.map(function (a) { return doc.getElementById(a.getAttribute("href").slice(1)); });
    var active = -2;
    function sync(force) {
      var line = window.innerHeight * 0.32, cur = -1;
      targets.forEach(function (t, i) { if (t && t.getBoundingClientRect().top <= line) cur = i; });
      if (cur === active && !force) return;
      active = cur;
      links.forEach(function (a, i) { if (i === cur) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); });
      if (cur >= 0) {
        var a = links[cur], l = a.offsetLeft - 8, r = a.offsetLeft + a.offsetWidth + 8;
        if (l < seg.scrollLeft || r > seg.scrollLeft + seg.clientWidth) seg.scrollTo({ left: l, behavior: reduce ? "auto" : "smooth" });
      }
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return; ticking = true;
      window.requestAnimationFrame(function () { sync(false); ticking = false; });
    }, { passive: true });
    window.addEventListener("resize", function () { sync(true); }, { passive: true });
    sync(true);
  }

  function start() { autoNav(); theme(); scrollSpy(); }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
})();
