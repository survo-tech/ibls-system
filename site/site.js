/* India Business Leaders Survey: page enhancements. The page works without this file.
   Scroll spy, theme toggle, reveal on scroll, timeline progress, pointer parallax. No tracking. */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  root.classList.add("js");
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");

  function theme() {
    var btn = doc.getElementById("theme-btn"), mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (!btn) return;
    btn.hidden = false;
    function current() { var t = root.getAttribute("data-theme"); return t === "light" || t === "dark" ? t : (mq.matches ? "dark" : "light"); }
    function label() { btn.setAttribute("aria-label", current() === "dark" ? "Switch to light theme" : "Switch to dark theme"); }
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("ibls-theme", next); } catch (e) { /* storage blocked */ }
      label();
    });
    if (mq.addEventListener) mq.addEventListener("change", label);
    label();
  }

  // Scroll progress bar, nav highlight and timeline fill share one rAF-throttled handler.
  function scroll() {
    var bar = doc.getElementById("progress"), tl = doc.getElementById("tl");
    var seg = doc.querySelector(".seg"), links = seg ? [].slice.call(seg.querySelectorAll("a")) : [];
    var targets = links.map(function (a) { return doc.getElementById(a.getAttribute("href").slice(1)); });
    var active = -2, ticking = false;
    function update() {
      ticking = false;
      var max = root.scrollHeight - window.innerHeight;
      if (bar) bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, window.scrollY / max) : 0) + ")";
      if (tl) {
        var list = tl.querySelector(".tl__list"), r = list.getBoundingClientRect(), vh = window.innerHeight;
        var p = reduce.matches ? 1 : Math.max(0, Math.min(1, (vh * 0.6 - r.top) / r.height));
        tl.style.setProperty("--p", p.toFixed(3));
      }
      var line = window.innerHeight * 0.32, cur = -1;
      targets.forEach(function (t, i) { if (t && t.getBoundingClientRect().top <= line) cur = i; });
      if (cur !== active) {
        active = cur;
        links.forEach(function (a, i) { if (i === cur) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); });
        if (cur >= 0) {
          var a = links[cur], l = a.offsetLeft - 8, rr = a.offsetLeft + a.offsetWidth + 8;
          if (l < seg.scrollLeft || rr > seg.scrollLeft + seg.clientWidth) seg.scrollTo({ left: l, behavior: reduce.matches ? "auto" : "smooth" });
        }
      }
    }
    function req() { if (!ticking) { ticking = true; window.requestAnimationFrame(update); } }
    window.addEventListener("scroll", req, { passive: true });
    window.addEventListener("resize", req, { passive: true });
    update();
  }

  // Fade items in as they enter the viewport; count-up for the facts strip.
  function reveal() {
    var items = [].slice.call(doc.querySelectorAll(".rv, .tl__list li"));
    function countUp(el) {
      var to = parseInt(el.getAttribute("data-count"), 10), start = null;
      if (!to || reduce.matches) return;
      function step(t) {
        if (start === null) start = t;
        var k = Math.min(1, (t - start) / 900);
        el.textContent = String(Math.round(to * (1 - Math.pow(1 - k, 3))));
        if (k < 1) window.requestAnimationFrame(step);
      }
      el.textContent = "0";
      window.requestAnimationFrame(step);
    }
    if (reduce.matches || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    root.classList.add("rv-on");
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("in");
        [].slice.call(e.target.querySelectorAll("[data-count]")).forEach(countUp);
        io.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  }

  // Hero diagram layers drift slightly with the pointer.
  function parallax() {
    var art = doc.getElementById("art");
    if (!art || reduce.matches || !window.matchMedia("(pointer: fine)").matches) return;
    var hero = art.closest(".hero") || art;
    hero.addEventListener("pointermove", function (e) {
      var r = art.getBoundingClientRect();
      art.style.setProperty("--px", (((e.clientX - r.left) / r.width - 0.5) * 2).toFixed(3));
      art.style.setProperty("--py", (((e.clientY - r.top) / r.height - 0.5) * 2).toFixed(3));
    });
    hero.addEventListener("pointerleave", function () { art.style.setProperty("--px", "0"); art.style.setProperty("--py", "0"); });
  }

  function start() { theme(); scroll(); reveal(); parallax(); }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
})();
