(() => {
  "use strict";

  // Status endpoint (set in index.html):
  //   window.HOSTBOT_STATUS_URL = "https://your-vps.example.com";
  const STATUS_URL = (window.HOSTBOT_STATUS_URL || "").replace(/\/+$/, "");

  // ---------- Terminal deploy animation ----------
  const progressBar = document.querySelector(".progress-bar");
  const successMsg = document.querySelector(".success");

  function deployAnimation() {
    let progress = 0;
    if (progressBar) progressBar.style.width = "0%";
    if (successMsg) successMsg.style.opacity = "0";

    const interval = setInterval(() => {
      progress += Math.random() * 8;
      if (progress >= 100) {
        progress = 100;
        clearInterval(interval);
        if (successMsg) successMsg.style.opacity = "1";
      }
      if (progressBar) progressBar.style.width = progress + "%";
    }, 120);
  }

  deployAnimation();
  setInterval(deployAnimation, 8000);

  // ---------- Reveal on scroll ----------
  const revealEls = document.querySelectorAll(
    ".card, .step, .admin-block, .cmd-list li"
  );
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
  const heroStatusText = document.getElementById("heroStatusText");
  const heroUptime = document.getElementById("heroUptime");
  const heroBots = document.getElementById("heroBots");
  const heroPending = document.getElementById("heroPending");

  const big = document.getElementById("statusBig");
  const hint = document.getElementById("statusHint");
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  };

  function setState(state, label) {
    if (heroStatusText) heroStatusText.textContent = label;
    if (big) {
      big.className =
        "status-big " + (state === "online" ? "online" : state === "offline" ? "offline" : "");
      const span = big.querySelector("span:last-child");
      if (span) span.textContent = label;
    }
  }

  if (!STATUS_URL) {
    setState("offline", "● Status endpoint not configured");
    if (hint) hint.textContent = "Set window.HOSTBOT_STATUS_URL in web/index.html to show live data.";
    return;
  }

  fetch(STATUS_URL + "/health", {
    headers: { Accept: "application/json" },
    mode: "cors",
  })
    .then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    })
    .then((data) => {
      setState("online", "● All Systems Operational");
      if (heroUptime) heroUptime.textContent = data.uptime || "99.99%";
      if (heroBots) heroBots.textContent = data.running_bots ?? "—";
      if (heroPending) heroPending.textContent = data.pending_files ?? "—";
      set("stUptime", data.uptime || "—");
      set("stUsers", data.total_users ?? "—");
      set("stBots", data.running_bots ?? "—");
      set("stPending", data.pending_files ?? "—");
      if (hint) hint.textContent = "Live data from your VPS status server.";
    })
    .catch(() => {
      setState("offline", "● Offline — could not reach status server");
      if (hint)
        hint.textContent =
          "Could not reach the status endpoint. Check your VPS firewall / status server.";
    });
})();