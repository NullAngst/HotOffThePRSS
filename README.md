# Hot Off The PRSS

> RSS feeds → Discord. No limits, no fuss, self-hosted.

Hot Off The PRSS watches any number of RSS feeds and posts new articles to Discord channels as clean rich embeds. One feed can go to multiple channels. One channel can receive multiple feeds. You manage everything from a web dashboard — no config file editing required.

![Dark mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/dark.png?raw=true)
![Light mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/light.png?raw=true)

---

## Features

**Web dashboard** — Add, edit, pause, force-check, and delete feeds from your browser. Dark/light mode included.

**Multi-webhook routing** — Send one feed to as many Discord channels or servers as you want. Each destination gets its own label so you know where things are going.

**Role-based access control** — Three tiers: Owner (full control), Super Admin (feed + user management), and Admin (feeds only). Passwords are salted and hashed with scrypt.

**Per-webhook memory** — Each destination tracks what it has already sent independently. Add a new channel to an existing feed and it'll only receive articles published from that point forward — no retroactive spam.

**24-hour rolling window** — Only articles from the last 24 hours are ever considered. New feeds are silently seeded on first run so there's no initial flood.

**Live status badges** — Color-coded per feed: green (healthy), yellow (redirected), red (error), grey (paused).

**Backup & restore** — Download or upload your feed config and user database as JSON files. Useful for migrations and peace of mind.

**Atomic state management** — File locking and atomic writes prevent race conditions and duplicate posts under load.

**Lightweight** — Runs comfortably on a Raspberry Pi or a small VPS.

---

## How It Works

Two processes run side by side:

- **`main_web.py`** — A Flask/Gunicorn web server that serves the dashboard and handles all configuration.
- **`scheduler.py`** — A background process that wakes up on each feed's configured interval, checks for new articles, and fires webhooks.

They communicate via the shared `config.json` file. You never need to edit that file by hand.

When running under Docker, both processes are launched inside a single container by `docker_entrypoint.sh` — the scheduler runs in the background and Gunicorn runs in the foreground. Data is persisted via a mounted volume so your feeds and accounts survive container restarts and rebuilds.

---

## Requirements

**Without Docker:**
- Python 3.8+
- `pip` and `venv`
- A Linux, macOS, or Windows host (Linux recommended for production)

**With Docker:**
- Docker and Docker Compose

**Either way:**
- One or more Discord webhook URLs

---

## Installation

### Clone the repository

```bash
git clone https://github.com/NullAngst/HotOffThePRSS.git
cd HotOffThePRSS
```

Then follow either the **Docker** or **Manual** path below.

---

## Running with Docker (recommended)

Docker is the easiest way to run Hot Off The PRSS. Both the web UI and scheduler start automatically inside a single container, and your data lives on the host so nothing is lost when you update.

### 1. Build and start the container

```bash
cd path/to/your/docker/setup
docker build -t your-image-name .
docker run -p your-port:your-port your-image-name
```

The web UI will be available at `http://<your-server-ip>:5000`.

### 2. Data persistence

Your `config.json` and `users.json` are stored in the host directory mapped by the volume. By default this is:

```
/data/compose/hotofftheprss/
```

Back this directory up to keep your feeds and accounts safe.

### 3. View logs

```bash
docker logs hotofftheprss
docker logs hotofftheprss --follow
```

### 4. Update to a new version

```bash
docker compose down
docker compose up -d --build
```

Your data directory is untouched by rebuilds.

---

## Running Manually

### 1. Set up a Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install feedparser PyYAML Flask gunicorn requests werkzeug
```

### 2. Get a Discord webhook URL

In Discord: **Channel Settings → Integrations → Webhooks → Create Webhook**. Copy the URL. Repeat for each channel you want to post to.

### Development / quick test

```bash
# Terminal 1 — web UI
python main_web.py

# Terminal 2 — scheduler
python scheduler.py
```

Then open `http://localhost:5000` in your browser.

### Production (systemd)

Create a service file for the web UI:

```bash
sudo nano /etc/systemd/system/hotofftheprss-web.service
```

```ini
[Unit]
Description=Hot Off The PRSS — Web UI
After=network.target

[Service]
User=your_user
WorkingDirectory=/home/your_user/HotOffThePRSS
Environment="PATH=/home/your_user/HotOffThePRSS/venv/bin"
ExecStart=/home/your_user/HotOffThePRSS/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:5000 "main_web:app"
Restart=always

[Install]
WantedBy=multi-user.target
```

Create a service file for the scheduler:

```bash
sudo nano /etc/systemd/system/hotofftheprss-scheduler.service
```

```ini
[Unit]
Description=Hot Off The PRSS — Scheduler
After=network.target

[Service]
User=your_user
WorkingDirectory=/home/your_user/HotOffThePRSS
ExecStart=/home/your_user/HotOffThePRSS/venv/bin/python scheduler.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start both:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hotofftheprss-web.service
sudo systemctl enable --now hotofftheprss-scheduler.service
```

Check logs if something isn't working:

```bash
sudo journalctl -u hotofftheprss-web -n 50 --no-pager
sudo journalctl -u hotofftheprss-scheduler -n 50 --no-pager
```

---

## First-Time Setup

1. Navigate to `http://<your-server-ip>:5000`
2. You'll be prompted to create an **Owner** account. This only appears once.
3. Log in and you'll land on the main dashboard.

---

## Usage

### Adding a feed

1. Click **Add New Feed**
2. Fill in:
   - **Name** — a friendly label (e.g. `Ars Technica`)
   - **RSS URL** — the direct feed URL
   - **Webhook destinations** — one or more Discord webhook URLs, each with an optional label (e.g. `Tech Server — #news`)
   - **Refresh interval** — how often to check, in seconds (e.g. `300` for every 5 minutes)
3. Save. The scheduler will pick it up on its next cycle.

### Feed status indicators

| Badge | Meaning |
|---|---|
| 🟢 Green | Feed is healthy |
| 🟡 Yellow | Feed URL is redirecting — consider updating it |
| 🔴 Red | Feed is returning an error (4xx/5xx) |
| ⚫ Grey | Feed is paused |

### Pausing a feed

Click **Edit** on any feed and toggle it inactive. The scheduler will skip it until you re-enable it. No articles are lost from other feeds.

### Force-checking a feed

Hit **Force Check** on any feed to trigger an immediate check outside the normal schedule. Useful when testing a new webhook URL.

---

## Backup & Restore

Navigate to **Backup / Restore** in the dashboard.

- **Config backup** — downloads `config.json` with all your feeds and webhook destinations
- **User DB backup** *(Owner only)* — downloads the encrypted `users.json` to migrate accounts between installs
- **Restore** — upload either file to overwrite the current state; the scheduler picks up changes automatically

---

## Migrating from an older config format

If you have a `config.json` from a previous version with duplicate feed entries or the old flat webhook format, run the included migration script:

```bash
bash convert_config.sh
```

This merges duplicate feed URLs into single entries, de-duplicates webhooks, and writes the result to `config_merged.json`. Review that file, then:

```bash
mv config_merged.json config.json
```

Requires `jq` (`sudo apt install jq`).

---

## User Roles

| Role | Feeds | Users | Backup |
|---|---|---|---|
| **Owner** | ✅ Full | ✅ Full (all users) | ✅ Config + User DB |
| **Super Admin** | ✅ Full | ✅ Admins only | ✅ Config only |
| **Admin** | ✅ Full | ❌ | ❌ |

The Owner account cannot be deleted or demoted.

---

## Configuration Files

All config files are created and managed automatically in the project directory. You never need to edit them by hand.

| File | Purpose |
|---|---|
| `config.json` | Feed list, webhook destinations, intervals, sent-article memory |
| `users.json` | Hashed user credentials and roles |
