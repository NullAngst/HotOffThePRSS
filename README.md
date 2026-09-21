# Hot Off The PRSS

Self-hosted RSS and Atom to Discord relay. Point it at feeds, pick the channels each feed posts to, and manage it all from a web dashboard.

![Dark mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/dark.png?raw=true)
![Light mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/light.png?raw=true)

## Features

- **Web dashboard** with live status, search, filters, drag-to-reorder (saved on the server), and light and dark themes.
- **Multiple destinations per feed.** One feed can post to any number of Discord channels, and one channel can receive any number of feeds.
- **No backlog floods.** When a feed is first paired with a destination, everything already in the feed is marked as seen. Only articles published after that are posted. This is tracked per feed and destination pair.
- **Reliable delivery.** An article is recorded as sent only after Discord accepts it. Rate limits are respected and retried; failed posts are retried on the next check instead of being lost.
- **Recent deliveries log** showing what was posted where, and what failed and why.
- **Feed health.** Errors, timeouts, non-feed responses and permanent redirects are shown per feed, with a one-click switch to a redirected feed's new address.
- **Preview and test tools.** Preview a feed before saving it, and send a test message to a webhook from the form.
- **Roles.** Owner, Super Admin and Admin. Passwords are salted and hashed.
- **Backup and restore** of feeds and users as JSON. Backups from older versions restore fine.
- **Automatic upgrades.** Data from older versions is migrated on first start, with backups kept.

## How it works

Two processes run side by side:

- `main_web.py` is the Flask web UI, served by gunicorn.
- `scheduler.py` checks due feeds (several in parallel), posts new articles, and records results.

They share a data directory:

| File | Contents |
|---|---|
| `config.json` | Feeds, destinations, intervals, feed order |
| `user.json` | Accounts with hashed passwords |
| `secret.key` | Session signing key |
| `prss_state.db` | SQLite: seen-article memory, feed health, delivery log, scheduler heartbeat |

The data directory is the project folder by default. Set `PRSS_DATA_DIR` to put it elsewhere (Docker uses `/data`).

## Running with Docker

```bash
git clone https://github.com/NullAngst/HotOffThePRSS.git
cd HotOffThePRSS/Docker
docker compose up -d --build
```

The dashboard is at `http://<server>:5000`. Data is stored in `/data/compose/hotofftheprss` on the host. Change the left side of the `volumes:` line in `docker-compose.yml` to use another folder.

The application code is built into the image and only data lives on the volume, so the host folder can start empty. The container restarts the scheduler if it ever exits and reports health through `/healthz`.

Logs: `docker logs -f hotofftheprss`

Update:

```bash
cd HotOffThePRSS && git pull
cd Docker && docker compose up -d --build
```

### Portainer

1. Stacks, Add stack, Build method: Repository.
2. Repository URL `https://github.com/NullAngst/HotOffThePRSS.git`, reference `refs/heads/main`, compose path `Docker/docker-compose.yml`.
3. Deploy. No need to clone the repository onto the host first anymore.

To update, open the stack and choose Pull and redeploy.

## Running without Docker

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r Docker/requirements.txt

# terminal 1
gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 main_web:app
# terminal 2
python scheduler.py
```

For systemd, create `/etc/systemd/system/hotofftheprss-web.service`:

```ini
[Unit]
Description=Hot Off The PRSS web UI
After=network.target

[Service]
User=your_user
WorkingDirectory=/home/your_user/HotOffThePRSS
ExecStart=/home/your_user/HotOffThePRSS/venv/bin/gunicorn --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:5000 main_web:app
Restart=always

[Install]
WantedBy=multi-user.target
```

and `/etc/systemd/system/hotofftheprss-scheduler.service`:

```ini
[Unit]
Description=Hot Off The PRSS scheduler
After=network.target

[Service]
User=your_user
WorkingDirectory=/home/your_user/HotOffThePRSS
ExecStart=/home/your_user/HotOffThePRSS/venv/bin/python scheduler.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl daemon-reload && sudo systemctl enable --now hotofftheprss-web hotofftheprss-scheduler`.

## Upgrading from an earlier version

Stop the old version, update the code, start the new one. Nothing needs converting by hand.

On first start the new version:

- Normalizes `config.json`: legacy `webhook_url` and `webhook_urls` fields become `webhooks`, missing or duplicate feed ids are fixed, string values are cleaned up. The original is kept as `config.json.pre-v2.bak`.
- Fixes `user.json` from the single-user era (missing id and role). The original is kept as `user.json.pre-v2.bak`.
- Imports `sent_articles.yaml` and `feed_state.json` into `prss_state.db` and renames them to `*.migrated`. Destinations that had already been receiving a feed keep going without reposting anything.
- Moves any custom feed order saved in your browser onto the server the first time you open the dashboard.

`convert_config.sh` is no longer needed and has been removed.

**Docker users:** the volume used to be mounted over the code directory (`/usr/src/app`). It is now mounted at `/data`. The provided `docker-compose.yml` already uses the same host folder, so your existing files are found. If you changed the host path, keep your path and only change the container side to `/data`.

**Rolling back** is possible: rename the `*.migrated` files back and restore the `.pre-v2.bak` files. An older version started without them seeds every destination again, so it does not flood channels either.

## Using it

**Add a feed.** Enter the feed address and choose Preview to confirm it loads. Name is optional. Add one or more Discord webhooks (Discord: Channel settings, Integrations, Webhooks) and use Send test to confirm each. Choose how often to check.

**Status.**

| Status | Meaning |
|---|---|
| Healthy | Last check succeeded |
| Moved | The feed redirects to a new address. Open the row to switch to it |
| Error text | The last check failed; the text says why |
| Waiting for first check | Added but not checked yet |
| Paused | Skipped until resumed |

A red delivery message means Discord refused or could not be reached. A 404 from Discord usually means the webhook was deleted.

**Check now** queues an immediate check. The dashboard updates when it finishes.

**Order.** Drag the handle on the left of a row, or focus it and use the arrow keys. The order is saved for everyone and included in backups.

## Roles

| Role | Feeds | Users | Backups |
|---|---|---|---|
| Owner | Yes | Everyone | Feeds and users |
| Super Admin | Yes | Admins only | Feeds |
| Admin | Yes | No | No |

The Owner can not be deleted or demoted. Changing or resetting a password signs that account out everywhere else. Repeated failed sign-ins are slowed down.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PRSS_DATA_DIR` | project folder | Where data files live |
| `PRSS_SECURE_COOKIES` | `0` | Set to `1` when served over HTTPS |
| `PRSS_TRUST_PROXY` | `0` | Set to `1` only behind your own reverse proxy, so client addresses are read correctly |
| `PRSS_WORKERS` | `4` | Feeds checked in parallel |
| `PRSS_MAX_POSTS_PER_CHECK` | `20` | Per destination. Beyond this the oldest new articles are skipped and logged, which guards against a feed that suddenly changes all its article ids |
| `PRSS_USER_AGENT` | Firefox string | User-Agent for feed requests |

## Notes and limits

- Dated articles older than 24 hours are never posted. Undated articles are posted when they first appear after a feed is set up.
- Seen-article memory for an article is kept for 30 days after it was last present in the feed.
- Config backups contain full webhook addresses. Treat them like passwords.
- The login throttle keys on client address and username. Behind a reverse proxy without `PRSS_TRUST_PROXY=1`, every client shares one address.
