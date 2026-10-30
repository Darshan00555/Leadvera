// Small progressive enhancements; every page works without JS.
(function () {
  // Bar-chart tooltips: any element with data-tip shows it on hover / focus.
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  function show(e) {
    const t = e.currentTarget.getAttribute("data-tip");
    if (!t) return;
    tip.textContent = t;
    tip.classList.add("on");
    move(e);
  }
  function move(e) {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX || r.left + r.width / 2;
    const y = e.clientY || r.top;
    tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, x + 12) + "px";
    tip.style.top = Math.max(8, y - tip.offsetHeight - 10) + "px";
  }
  function hide() { tip.classList.remove("on"); }
  document.querySelectorAll("[data-tip]").forEach(function (el) {
    el.addEventListener("mouseenter", show);
    el.addEventListener("mousemove", move);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("focus", show);
    el.addEventListener("blur", hide);
  });

  // Light / dark switch; the choice is remembered in this browser only.
  const root = document.documentElement;
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      const cur = root.getAttribute("data-theme") ||
        (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) { /* not persisted */ }
    });
  });

  // "/" focuses the global search; Esc closes the mobile menu and the account menu.
  const search = document.querySelector("[data-search]");
  const navToggle = document.getElementById("navtoggle");
  const userMenu = document.querySelector(".usermenu");
  document.addEventListener("keydown", function (e) {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
    if (e.key === "/" && search && !typing) { e.preventDefault(); search.focus(); }
    if (e.key === "Escape") {
      if (navToggle) navToggle.checked = false;
      if (userMenu) userMenu.removeAttribute("open");
    }
  });
  document.addEventListener("click", function (e) {
    if (userMenu && userMenu.open && !userMenu.contains(e.target)) userMenu.removeAttribute("open");
  });

  // Confirm destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Live run progress: poll the status API while a run is queued/running.
  const live = document.querySelector("[data-live-run]");
  if (live) {
    const id = live.getAttribute("data-live-run");
    const tick = function () {
      fetch("/api/runs/" + id + "/status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status !== "queued" && d.status !== "running") { window.location.reload(); return; }
          const s = d.stats || {};
          const set = function (k, v) { const el = live.querySelector("[data-k='" + k + "']"); if (el) el.textContent = v; };
          set("status", d.status);
          set("new_verified", s.new_verified || 0);
          set("candidates", s.candidates || 0);
          set("web_verified", s.web_verified || 0);
          const list = document.querySelector("[data-live-recent]");
          if (list && d.recent && d.recent.length) {
            list.textContent = "";
            d.recent.forEach(function (f) {
              const row = document.createElement("div");
              row.className = "item";
              const av = document.createElement("span");
              av.className = "avatar co";
              av.textContent = (f.name || f.overseas || "?").split(/\s+/).slice(0, 2).map(function (w) { return w.charAt(0); }).join("").toUpperCase();
              const body = document.createElement("div");
              body.style.cssText = "flex:1;min-width:0";
              const link = document.createElement("a");
              link.href = "/leads/" + f.id;
              link.style.fontWeight = "600";
              link.textContent = f.name || f.overseas;
              const sub = document.createElement("div");
              sub.className = "small muted";
              sub.textContent = f.lead_id + " · " + f.overseas + " · " + (f.country === "GB" ? "UK" : f.country);
              body.append(link, sub);
              const badge = document.createElement("span");
              badge.className = "badge " + (f.status === "Verified" ? "good" : f.status === "Web-verified" ? "info" : "");
              badge.textContent = f.status;
              row.append(av, body, badge);
              list.appendChild(row);
            });
            list.closest(".card").hidden = false;
          }
          set("cost", (d.cost_usd || 0).toFixed(2));
          const log = document.getElementById("runlog");
          if (log && d.log_tail) log.textContent = d.log_tail.join("\n");
          setTimeout(tick, 8000);
        })
        .catch(function () { setTimeout(tick, 20000); });
    };
    setTimeout(tick, 5000);
  }
})();
