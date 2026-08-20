(() => {
  "use strict";

  // Status endpoint (set in index.html):
  //   window.HOSTBOT_STATUS_URL = "https://your-vps.example.com";
  const STATUS_URL = (window.HOSTBOT_STATUS_URL || "").replace(/\/+$/, "");

  // ---------- Nav ----------
  const nav = document.getElementById("nav");
  const burger = document.getElementById("navBurger");

  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  if (burger) {
    burger.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      burger.setAttribute("aria-expanded", String(open));
    });
  }
  document.querySelectorAll(".nav-links a").forEach((a) =>
    a.addEventListener("click", () => nav.classList.remove("open"))
  );

  // ---------- Reveal on scroll ----------
  const revealEls = document.querySelectorAll(".card, .step, .admin-block, .cmd-list li");
  if ("IntersectionObserver" in window) {
    revealEls.forEach((el) => el.classList.add("reveal"));
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("visible");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    revealEls.forEach((el) => io.observe(el));
  }

  // ---------- Live status ----------
  const pill = document.getElementById("statusPill");
  const pillText = document.querySelector("#statusPill .status-text");
  const big = document.getElementById("statusBig");
  const hint = document.getElementById("statusHint");
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

  function setState(state, label) {
    pill.dataset.state = state;
    pillText.textContent = label;
    big.className = "status-big " + (state === "online" ? "online" : state === "offline" ? "offline" : "");
    const bigText = big.querySelector("span:last-child");
    if (bigText) bigText.textContent = label;
  }

  if (!STATUS_URL) {
    setState("offline", "Status endpoint not configured");
    return;
  }

  fetch(STATUS_URL + "/health", { headers: { Accept: "application/json" }, mode: "cors" })
    .then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    })
    .then((data) => {
      setState("online", "Online · HostBot is live");
      set("stUptime", data.uptime || "—");
      set("stUsers", data.total_users ?? "—");
      set("stBots", data.running_bots ?? "—");
      set("stPending", data.pending_files ?? "—");
      if (hint) hint.textContent = "Live data from your VPS status server.";
    })
    .catch(() => {
      setState("offline", "Offline · could not reach status server");
      if (hint) hint.textContent = "Could not reach the status endpoint. Check your VPS firewall / status server.";
    });
})();