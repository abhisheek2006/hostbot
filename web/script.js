(() => {
  "use strict";

  // Status endpoint (set in index.html):
  //   window.HOSTBOT_STATUS_URL = "https://your-vps.example.com";
  const STATUS_URL = (window.HOSTBOT_STATUS_URL || "").replace(/\/+$/, "");
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------- Nav ----------
  const nav = document.getElementById("nav");
  const burger = document.getElementById("navBurger");

  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 10);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  burger.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    burger.setAttribute("aria-expanded", String(open));
  });
  document.querySelectorAll("#navLinks a").forEach((a) =>
    a.addEventListener("click", () => {
      nav.classList.remove("open");
      burger.setAttribute("aria-expanded", "false");
    })
  );

  // ---------- Reveal on scroll ----------
  const revealEls = document.querySelectorAll(
    ".card, .step, .cmd-panel, .stack-card, .status-card"
  );
  if ("IntersectionObserver" in window) {
    revealEls.forEach((el, i) => el.style.setProperty("--d", `${(i % 4) * 70}ms`));
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("in");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
    );
    revealEls.forEach((el) => io.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add("in"));
  }

  // ---------- Terminal typing sequence ----------
  const lines = document.querySelectorAll(".terminal .tline");
  const bar = document.getElementById("tbar");
  const successLine = document.querySelector(".terminal .t-success");

  function runTerminal() {
    if (REDUCED) {
      lines.forEach((l) => (l.style.opacity = 1));
      if (bar) bar.style.width = "100%";
      if (successLine) successLine.style.opacity = 1;
      return;
    }
    lines.forEach((l) => (l.style.animation = "none"));
    void document.body.offsetWidth; // reflow to restart animations
    lines.forEach((l, i) => {
      l.style.animation = `tlineIn 0.45s cubic-bezier(0.16,1,0.3,1) forwards`;
      l.style.animationDelay = `${0.3 + i * 0.55}s`;
    });
    const barDelay = 0.3 + lines.length * 0.55;
    setTimeout(() => {
      if (bar) bar.style.width = "100%";
    }, barDelay * 1000);
    setTimeout(() => {
      if (successLine) successLine.style.opacity = 1;
    }, (barDelay + 1.1) * 1000);
  }

  runTerminal();
  if (!REDUCED) setInterval(runTerminal, 9000);

  // ---------- Tilt cards ----------
  const tiltCards = document.querySelectorAll(".card, .step");
  if (!REDUCED && window.matchMedia("(pointer: fine)").matches) {
    tiltCards.forEach((card) => {
      card.addEventListener("mousemove", (e) => {
        const r = card.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width - 0.5;
        const py = (e.clientY - r.top) / r.height - 0.5;
        card.style.transform = `perspective(900px) rotateY(${px * 5}deg) rotateX(${py * -5}deg) translateY(-3px)`;
      });
      card.addEventListener("mouseleave", () => {
        card.style.transform = "";
      });
    });
  }

  // ---------- Magnetic primary CTA ----------
  const magnet = document.querySelector(".magnetic");
  if (magnet && !REDUCED && window.matchMedia("(pointer: fine)").matches) {
    magnet.addEventListener("mousemove", (e) => {
      const r = magnet.getBoundingClientRect();
      const x = (e.clientX - r.left - r.width / 2) * 0.22;
      const y = (e.clientY - r.top - r.height / 2) * 0.3;
      magnet.style.transform = `translate(${x}px, ${y}px)`;
    });
    magnet.addEventListener("mouseleave", () => {
      magnet.style.transform = "";
    });
  }

  // ---------- Live status ----------
  const statusCard = document.getElementById("statusCard");
  const statusText = document.getElementById("statusText");
  const heroUptime = document.getElementById("heroUptime");
  const heroBots = document.getElementById("heroBots");
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.textContent = v;
  };

  function setState(state, label) {
    statusCard.classList.remove("online", "offline");
    statusCard.classList.add(state);
    if (statusText) statusText.textContent = label;
  }

  if (!STATUS_URL) {
    setState("offline", "Status endpoint not configured");
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
      setState("online", "All systems operational");
      if (heroUptime) heroUptime.textContent = data.uptime || "99.99%";
      if (heroBots) heroBots.textContent = (data.running_bots ?? "0") + " bots online";
      set("stUptime", data.uptime || "-");
      set("stUsers", data.total_users ?? "-");
      set("stBots", data.running_bots ?? "-");
      set("stPending", data.pending_files ?? "-");
      const hint = document.getElementById("statusHint");
      if (hint) hint.textContent = "Live data from your VPS status server.";
    })
    .catch(() => {
      setState("offline", "Could not reach status server");
    });
})();