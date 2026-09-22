# Hot Off The PRSS

Self-hosted RSS and Atom relay for Discord, Slack, Mattermost, Matrix, Telegram, ntfy, Gotify and plain webhooks. Point it at feeds, pick where each feed posts, and manage it all from a web dashboard.

![Dark mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/dark.png?raw=true)
![Light mode preview](https://raw.githubusercontent.com/NullAngst/HotOffThePRSS/refs/heads/main/light.png?raw=true)

## Features

- **Web dashboard** with live status, search, filters, drag-to-reorder (saved on the server), and light and dark themes.
- **Multiple destinations per feed, on any mix of services.** One feed can post to any number of destinations, and one destination can receive any number of feeds. See [Destinations](#destinations).
- **Several sources per feed.** One feed can pull from several addresses (for example four sections of the same forum) with one set of cookies, schedule and destinations. An article that appears in more than one source is posted once.
- **Feeds behind a login.** A feed can be fetched with cookies from a signed-in browser (for example a XenForo forum) and a custom User-Agent.
- **Several sources.** In a feed's Source section, choose **Add another source** for each extra address. Every source uses the feed's name, cookies, User-Agent, schedule and destinations. Articles are merged, and one that appears in several sources is posted once. When a source is added to a feed that is already running, its existing articles are marked as seen, not posted; only later ones are. Each source's health is shown in the feed's details, and one failing source does not stop the others from posting.

**Edit all.** Change the check interval or pause and resume many feeds at once.
- **No backlog floods.** When a feed is first paired with a destination, everything already in the feed is marked as seen. Only articles published after that are posted. This is tracked per feed and destination pair.
- **Reliable delivery.** An article is recorded as sent only after the destination accepts it. Rate limits are respected and retried; failed posts are retried on the next check instead of being lost.
- **Recent deliveries log** showing what was posted where, and what failed and why.
- **Feed health.** Errors, timeouts, non-feed responses and permanent redirects are shown per feed, with a one-click switch to a redirected feed's new address.
- **Preview and test tools.** Preview a feed before saving it, and send a test message to any destination from the form.
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

- Upgrades `config.json` to the current format (version 4). Legacy `webhook_url` and `webhook_urls` fields become `webhooks`, missing or duplicate feed ids are fixed, string values are cleaned up, and every existing destination gets a service type (Discord, or Slack for `hooks.slack.com` addresses). The original is kept as `config.json.pre-v4.bak` (earlier upgrades left `.pre-v3.bak` or `.pre-v2.bak`).
- Upgrades `prss_state.db` to per-source state. Each feed's health and cache headers move to its first source, so existing feeds carry on without re-seeding.
- Fixes `user.json` from the single-user era (missing id and role). The original is kept as `user.json.pre-v2.bak`.
- Imports `sent_articles.yaml` and `feed_state.json` into `prss_state.db` and renames them to `*.migrated`. Destinations that had already been receiving a feed keep going without reposting anything.
- Moves any custom feed order saved in your browser onto the server the first time you open the dashboard.

`convert_config.sh` is no longer needed and has been removed.

**Docker users:** the volume used to be mounted over the code directory (`/usr/src/app`). It is now mounted at `/data`. The provided `docker-compose.yml` already uses the same host folder, so your existing files are found. If you changed the host path, keep your path and only change the container side to `/data`.

**Rolling back** is possible: rename any `*.migrated` files back and restore the newest `.pre-v*.bak` files. An older version started without them seeds every destination again, so it does not flood channels either. Versions before destination types treat every destination as a Discord webhook, so restore the backup rather than running an older version against a config that uses other services. Older versions also fetch only the first source of a feed with several.

## Using it

**Add a feed.** Enter the feed address and choose Preview to confirm it loads. Name is optional. Add one or more destinations, pick the service for each, and use Send test to confirm it works. Choose how often to check.

**Edit all.** The Edit all button on the dashboard changes the check interval, the active state, or both, for any set of feeds. Feeds can be picked by their current interval (hold Shift to add another interval group), filtered by name, or selected all at once.

**Status.**

| Status | Meaning |
|---|---|
| Healthy | Last check succeeded |
| Moved | The feed redirects to a new address. Open the row to switch to it |
| Error text | The last check failed; the text says why |
| Waiting for first check | Added but not checked yet |
| Paused | Skipped until resumed |

A red delivery message means the destination refused the post or could not be reached. A 401, 403 or 404 usually means a deleted webhook, a revoked token or a bot that lost access; those articles stay queued and are retried once the destination is fixed.

If Check now or Send test report that a request was redirected or blocked before it reached the app, something in front of it (an authentication proxy such as Cloudflare Access, Authelia or Authentik, or a firewall) intercepted it. Check now then falls back to a normal page submit, which follows the redirect.

**Check now** queues an immediate check. The dashboard updates when it finishes.

**Order.** Drag the handle on the left of a row, or focus it and use the arrow keys. The order is saved for everyone and included in backups.

## Destinations

| Service | What it needs | Where to get it |
|---|---|---|
| Discord | Webhook URL | Channel settings, Integrations, Webhooks |
| Slack | Webhook URL | A Slack app with Incoming Webhooks enabled |
| Mattermost | Webhook URL | Integrations, Incoming Webhooks. Rocket.Chat incoming webhooks also work |
| Matrix | Homeserver, room ID (`!id:server`) or alias (`#room:server`), access token | A separate bot account that has joined the room. Messages are sent as notices |
| Telegram | Bot token, chat ID (`-100...` or `@channel`) | @BotFather. The bot must be in the group, or an admin of the channel. Forum topics are not supported |
| ntfy | Topic URL, optional access token | ntfy.sh or your own server |
| Gotify | Server URL, application token | An application created in Gotify |
| Generic webhook | URL | Receives JSON: `feed`, `title`, `link`, `summary`, `published` (ISO 8601), `image` |

Each destination remembers what it has received on its own. Discord destinations keep their memory from earlier versions.

## Feeds that need a login

Some feeds only show everything to signed-in members. Open the feed's **Access** section and paste the site's cookies.

For a XenForo forum:

1. Sign in with **Stay logged in** ticked, ideally in a private window.
2. Open the browser's developer tools, then Storage (Firefox) or Application (Chrome), then Cookies, and copy `xf_user`. Adding `xf_session` does no harm.
3. Paste as `xf_user=...; xf_session=...` and choose Preview to confirm the feed loads. To follow several sections, add each section's RSS address as another source on the same feed so they share the cookies. Close the private window without signing out: signing out invalidates the cookie.

The field also accepts one `name=value` per line, a cookies.txt export, or the JSON exported by browser cookie-editor extensions. Values are sent exactly as pasted.

Notes:

- Cookies are sent only to the feed's own host, never to a site it redirects to.
- If the site answers with a login page or a 401/403, the feed shows an error saying the cookies may have expired. Paste fresh ones.
- Changing a feed's cookies re-seeds its destinations, so threads that only become visible once signed in are marked as seen instead of being posted in a burst.
- Some sites tie a session to the browser that created it. If cookies alone do not work, also paste that browser's User-Agent.

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
| `PRSS_USER_AGENT` | Chrome string | Default User-Agent for feed requests (a feed's own setting overrides it) |
| `PRSS_TELEGRAM_API` | `https://api.telegram.org` | Only for a self-hosted Telegram Bot API server |

## Notes and limits

- Dated articles older than 24 hours are never posted. Undated articles are posted when they first appear after a feed is set up.
- Seen-article memory for an article is kept for 30 days after it was last present in the feed.
- Config backups contain full webhook addresses, tokens and feed cookies. Treat them like passwords.
- The login throttle keys on client address and username. Behind a reverse proxy without `PRSS_TRUST_PROXY=1`, every client shares one address.
