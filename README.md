# HostBot ▣

Host, run and manage **Python 3.14** & **Node.js** Telegram bots directly from Telegram.
Users upload a `.py`, `.js` or `.zip` project, admins approve it, and HostBot keeps it
running **24/7** — with one-tap start / stop / restart, live logs and automatic
`pip` / `npm` dependency installs.

- 🤖 Telegram bot → runs on your **VPS** (long-running, persistent storage)
- 🎨 Landing page → served by **Vercel** (static, ultra-fast)
- 📊 Optional live status → tiny JSON server on the VPS feeds the landing page

---

## Repository layout

```
├── bot/               # The Telegram bot (Python 3.14)
│   ├── hostbot.py     # Main bot (ported from the original H.py)
│   ├── requirements.txt
│   └── start.sh       # Linux VPS launcher (venv + run)
├── web/               # Vercel landing page (index.html, style.css, script.js)
├── vercel.json        # Vercel static-site config
├── Dockerfile         # VPS via Docker (Python 3.14 + Node.js)
├── docker-compose.yml # One-command Docker deployment
├── hostbot.service    # systemd unit (Docker-free VPS)
├── .env.example       # Configuration template (commit this)
└── .env               # Real secrets — NEVER commit
```

---

## 1. Configuration (`.env`)

Copy the template and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | Your numeric Telegram user id (full access) |
| `ADMIN_ID` | Co-admin numeric id (optional) |
| `YOUR_USERNAME` | e.g. `@yourusername` (contact button) |
| `UPDATE_CHANNEL` | URL of your updates channel |
| `A4F_API_KEY` | API key for the `/mpx` AI assistant (optional) |
| `DATA_DIR` | Storage dir — `./data` locally, `/data` on VPS/Docker |
| `STATUS_SERVER_ENABLED` | `true`/`false` — expose the live stats endpoint |
| `STATUS_SERVER_PORT` | Port for the status endpoint (default `9090`) |
| `STATUS_TOKEN` | Optional bearer token protecting the endpoint |

> ⚠️ `.env` is gitignored on purpose. Keep secrets out of the repository.

---

## 2. Deploy the bot to a VPS

### Option A — Docker (recommended)

```bash
# on your VPS
git clone https://github.com/abhisheek2006/hostbot.git
cd hostbot
cp .env.example .env          # fill in your token & ids
docker compose up -d --build
docker compose logs -f        # watch it boot
```

The container bundles **Python 3.14** and **Node.js**, mounts `./data` as a
persistent volume (uploads + SQLite DB survive restarts) and restarts automatically.

### Option B — systemd (no Docker)

```bash
sudo apt update
sudo apt install -y python3 python3-venv nodejs npm

sudo mkdir -p /opt/hostbot
sudo cp -r bot /opt/hostbot/       # copy the bot folder
sudo cp .env /opt/hostbot/.env     # your real config
sudo cp hostbot.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now hostbot
sudo systemctl status hostbot
```

To run manually: `cd bot && bash start.sh`.

---

## 3. Deploy the landing page to Vercel

The repo is already wired up with `vercel.json` — it builds nothing and just serves
the `web/` folder as a static site.

**Via dashboard:** push the repo → Vercel → *Import* → framework **Other** → *Deploy*.
**Via CLI:**

```bash
npm i -g vercel
vercel           # in the repo root, accept defaults
vercel --prod
```

### Live stats on the page (optional)

1. On the VPS, set `STATUS_SERVER_ENABLED=true` (and optionally `STATUS_TOKEN=...`).
2. Expose the port (e.g. `9090`) in your firewall.
3. In `web/index.html`, point the status URL at your VPS:

```html
<script>
  window.HOSTBOT_STATUS_URL = "https://bot.example.com";
</script>
```

The page then shows uptime, user count, running bots and pending files.

---

## 4. Using the bot

Start it with `/start`. Upload a `.py`, `.js` or `.zip` project, wait for admin
approval, then **Start** it from the file controls.

### User commands

| Command | Action |
| --- | --- |
| `/start` · `/help` | Main menu & help |
| `/uploadfile` | Upload a script/project |
| `/checkfiles` | Manage your bots |
| `/stats` | Statistics |
| `/speed` · `/uptime` | Speed test / uptime |
| `/mpx` | Ask the AI assistant |

### Admin / owner commands

| Command | Action |
| --- | --- |
| `/pending` | Review pending files |
| `/broadcast` | Message all active users |
| `/subscriptions` | Grant/remove premium slots |
| `/lockbot` | Lock the bot |
| `/adminpanel` | Manage admins (owner) |
| `/runningallcode` | Start every approved bot |

---

## 5. Local development

```bash
python3.14 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r bot/requirements.txt
python bot/hostbot.py
```

---

## License

Provided as-is. Use at your own risk — uploaded scripts run as subprocesses on your host.