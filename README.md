# HostBot ▣

Host, run and manage **Python 3.14** & **Node.js** Telegram bots directly from Telegram.
Users upload a `.py`, `.js` or `.zip` project, admins approve it, and HostBot keeps it
running **24/7** — with one-tap start / stop / restart, live logs and automatic
`pip` / `npm` dependency installs. All data lives in **MongoDB Atlas**.

- 🤖 Telegram bot → runs on your **VPS** (long-running, persistent storage)
- 🎨 Landing page → served by **Vercel** (static, ultra-fast)
- 📊 Dashboard API → tiny JSON server on the VPS powers the login + control panel
- 🗄️ Storage → **MongoDB Atlas** (plans, users, files, approvals)

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
├── deploy.sh          # One-shot systemd VPS setup (venv + deps + service)
├── .dockerignore      # Keeps the Docker build context small
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
| `MONGO_URI` | **Required** — MongoDB Atlas connection string |
| `MONGO_DB_NAME` | MongoDB database name (default `hostbot`) |
| `STATUS_SERVER_ENABLED` | `true`/`false` — expose the live stats + dashboard API |
| `STATUS_SERVER_PORT` | Port for the status endpoint (default `9090`) |
| `STATUS_TOKEN` | Optional bearer token protecting `/health` and `/stats` |
| `FREE_BOT_LIMIT` | Bots allowed for free users (default `3`) |
| `SUBSCRIBED_USER_LIMIT` | Fallback limit for paid users (default `15`) |
| `PLAN_STARTER_LIMIT` | `starter` plan bot limit (default `8`) |
| `PLAN_PRO_LIMIT` | `pro` plan bot limit (default `20`) |
| `PLAN_BUSINESS_LIMIT` | `business` plan bot limit (default `50`) |
| `WEB_USERNAME` | Web dashboard login (single user) |
| `WEB_PASSWORD` | Web dashboard password |
| `WEB_OWNER_ID` | Telegram id the single login maps to (defaults to `OWNER_ID`) |
| `WEB_USERS` | Multiple logins: `user1:pass1:id1;user2:pass2:id2` (overrides single login) |
| `WEB_SESSION_TTL` | Login session lifetime in seconds (default `86400`) |

> ⚠️ `.env` is gitignored on purpose. Keep secrets out of the repository.
>
> ⚠️ Avoid inline `#` comments after values (e.g. `KEY=value # note`).
> `python-dotenv` and Docker Compose strip them, but **systemd's
> `EnvironmentFile` does not** — keep comments on their own lines.

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
persistent volume (uploads survive restarts) and restarts automatically.

### Option B — systemd (no Docker)

Use the one-shot deploy script (creates the venv, installs dependencies,
installs the systemd unit and starts the service):

```bash
# on your VPS
git clone https://github.com/abhisheek2006/hostbot.git
cd hostbot
cp .env.example .env          # fill in your token & ids

sudo bash deploy.sh           # installs to /opt/hostbot
# or: sudo bash deploy.sh /srv/hostbot   for a custom location
```

Manual alternative (equivalent to what `deploy.sh` does):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm

sudo mkdir -p /opt/hostbot
sudo cp -r bot /opt/hostbot/       # copy the bot folder
sudo cp .env /opt/hostbot/bot/.env # your real config
python3 -m venv /opt/hostbot/bot/.venv
/opt/hostbot/bot/.venv/bin/pip install -r /opt/hostbot/bot/requirements.txt

sudo cp hostbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hostbot
sudo systemctl status hostbot     # follow logs: journalctl -u hostbot -f
```

To run manually: `cd bot && bash start.sh` (start.sh bootstraps the venv itself).

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

### Web dashboard (login + control panel)

The status server also hosts a per-user **dashboard API** (`/api/login`,
`/api/dashboard`, `/api/logs`, `/api/env`, `/api/bot`). The static pages
`web/login.html` and `web/dashboard.html` talk to it. Point them at your VPS:

```html
<script>
  window.HOSTBOT_API_URL = "https://bot.example.com";
</script>
```

Set that in `web/login.html` and `web/dashboard.html` (auth is handled via
`web/auth.js` — the token lives in `localStorage`).

What the dashboard gives each logged-in user:

- Their details: name, Telegram ID, plan, bot limit and plan expiry
- Every hosted bot: file, type, approval status, PID, running state
- **Log file** viewer per bot (last 64 KB, optional 3s auto-refresh)
- **Environment editor** per bot (writes `.env` in the bot's folder; keys must
  match `[A-Za-z_][A-Za-z0-9_]*`)
- **Start / stop / restart** controls (only approved files can run)
- Logout ends the server session

Only users listed in `WEB_USERS` (or the single `WEB_USERNAME`/`WEB_PASSWORD`
login mapped to `WEB_OWNER_ID`) can sign in, and each login is scoped to that
user's own bots. Requires `STATUS_SERVER_ENABLED=true`.

## 4. Registration (choose a plan)

Users create their own dashboard account and pick a plan, from **both** places:

- **In the bot:** `/register` or the **📝 Register** button → enter a username,
  a password, then pick a plan from the inline buttons.
- **On the website:** `web/register.html` → fill in username, Telegram ID,
  password and pick a plan (Free / Starter / Pro / Business).

Both call the same `register_web_user()` logic and store the account in the
`web_users` MongoDB collection (passwords are salted + hashed with PBKDF2).
The **Free** plan is active immediately. Paid plans are recorded as a request and
the owner is notified to activate them via `/subscriptions` → Add
(`ID 30 plan`).

After registering, users log in at `web/login.html` and land on their dashboard.

---

## 5. Hosting plans

Free users can host **3 bots** (configurable via `FREE_BOT_LIMIT`). Paid plans
unlock more, granted per user by admins:

| Plan | Bots |
| --- | --- |
| Free | 3 (default) |
| Starter | 8 (default) |
| Pro | 20 (default) |
| Business | 50 (default) |

Admins grant a plan when adding a subscription: `/subscriptions` → *Add*, format
`ID days plan` (e.g. `123456789 30 pro`). Users see their current plan and all
tiers with `/plans` or the **💠 Plans** button.

---

## 6. Using the bot

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
| `/plans` | View hosting plans |
| `/register` | Create a dashboard account (choose a plan) |
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

## 7. Local development

```bash
python3.14 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r bot/requirements.txt
python bot/hostbot.py
```

---

## License

Provided as-is. Use at your own risk — uploaded scripts run as subprocesses on your host.