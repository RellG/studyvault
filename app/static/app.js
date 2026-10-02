// studyvault: small shared helpers. Page-specific scripts live in their templates.

// Running study timer: elements with data-elapsed show seconds since the session started (server-side value
// at render time + time since page load), so a reload never loses the timer.
(function () {
  var nodes = document.querySelectorAll(".js-elapsed[data-elapsed]");
  var pomo = document.getElementById("pomo");
  if (!nodes.length && !pomo) return;
  var loaded = Date.now();
  function fmt(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  }
  function tick() {
    var dt = Math.floor((Date.now() - loaded) / 1000);
    nodes.forEach(function (n) { n.textContent = fmt(parseInt(n.dataset.elapsed, 10) + dt); });
    if (pomo) {
      var e = parseInt(pomo.dataset.elapsed, 10) + dt, c = e % 1800;  // 25 min focus + 5 min break
      pomo.textContent = c < 1500 ? "Focus · " + fmt(1500 - c) + " left" : "Break · " + fmt(1800 - c) + " left";
      pomo.className = "small badge " + (c < 1500 ? "accent" : "green");
    }
  }
  tick();
  setInterval(tick, 1000);
})();

// Double-submit guard: a second press (or a double tap on a phone) while a form is already on its way is
// ignored, so Accept, Apply, Grade and Finish happen once. Forms that handle submit themselves are skipped,
// as are forms marked data-resubmit. Coming back with the Back button re-arms them.
document.addEventListener("submit", function (e) {
  var f = e.target;
  if (e.defaultPrevented || f.hasAttribute("data-resubmit") || (f.method || "").toLowerCase() !== "post") return;
  if (f.dataset.sent) { e.preventDefault(); return; }
  f.dataset.sent = "1";
  f.setAttribute("aria-busy", "true");
});
window.addEventListener("pageshow", function () {
  document.querySelectorAll("form[data-sent]").forEach(function (f) { delete f.dataset.sent; f.removeAttribute("aria-busy"); });
});

// "More" menu in the top bar: closes on Escape, on a click elsewhere, and when focus moves out of it.
(function () {
  var more = document.querySelector("details.more");
  if (!more) return;
  document.addEventListener("click", function (e) { if (more.open && !more.contains(e.target)) more.open = false; });
  more.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && more.open) { more.open = false; more.querySelector("summary").focus(); }
  });
  more.addEventListener("focusout", function (e) {
    if (more.open && e.relatedTarget && !more.contains(e.relatedTarget)) more.open = false;
  });
})();

// Older forms write <label>Name</label><input> with no for=: tie each such label to the control right after it, so a tap
// on the label focuses the field and screen readers announce it.
(function () {
  var n = 0;
  document.querySelectorAll("label:not([for])").forEach(function (l) {
    var c = l.nextElementSibling;
    if (l.querySelector("input, select, textarea") || !c || !/^(INPUT|SELECT|TEXTAREA)$/.test(c.tagName)) return;
    if (!c.id) c.id = "field-" + (++n);
    l.htmlFor = c.id;
  });
})();
