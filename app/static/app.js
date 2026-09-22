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
