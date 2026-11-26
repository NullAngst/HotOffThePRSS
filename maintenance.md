# Maintenance

A checklist for keeping Hot Off The PRSS healthy and current. Run through it every couple of months, or right after a Python or Debian release.

## 1. Check the running instance

1. Open the dashboard. The masthead should say "Scheduler running".
2. Run `docker ps` (or look in Portainer). The container should show `healthy`. Unhealthy means the web UI or the scheduler is down.
3. Skim the logs for repeated errors: `docker logs --since 72h hotofftheprss 2>&1 | grep -v "GET /" | less`
4. Filter the dashboard by Needs attention. Fix or remove feeds that have failed for weeks, and switch Moved feeds to their new address.
5. Look at the Recent deliveries panel for red entries. "not retried, so it can not be posted twice" now and then is normal. Many of them in a row for one destination means it is slow or overloaded.

## 2. Update

The image is not pinned to any version, by design. Every deploy pulls the newest `python:3-slim` image and installs the newest release of each package in `Docker/requirements.txt`, since `docker-compose.yml` sets `pull: true` and `no_cache: true`.

1. Back up first: Backup page, download `config.json` and `user.json`. Or copy the data folder.
2. Pull and redeploy the stack in Portainer, or run `git pull && cd Docker && docker compose up -d --build`.
3. Watch the log for `[migrate]` lines and for errors during start: `docker logs -f hotofftheprss`
4. Repeat step 1 of the check above.

The trade-off of not pinning: a brand-new Python release can, rarely, break a package before the package catches up. If a build fails right after a new Python release:

1. Read the build error to find the package.
2. Change `FROM python:3-slim` in `Docker/Dockerfile` to the previous minor, for example `python:3.14-slim`.
3. Redeploy, and put it back to `python:3-slim` at the next check once the package has a release for the new Python.

## 3. Things that age and how to check them

| What | Where | How to check |
|---|---|---|
| Python packages | `Docker/requirements.txt` | Only lower bounds live here. Raise one if the code starts using a newer feature. Nothing else to do |
| Base image | `Docker/Dockerfile` | `python:3-slim` tracks the newest Python 3. Nothing to do unless a build breaks (see above) |
| GitHub Actions | `.github/workflows/checks.yml` | Compare `actions/checkout` and `actions/setup-python` with their Releases pages and move to the newest major tag |
| Default User-Agent | `scheduler.py`, `default_user_agent()` | Computed from the date on Chrome's four-week schedule. If Chrome changes its cadence, update `_CHROME_BASE` and the 132 base version |
| Destination APIs | `destinations.py` | Send test to one destination of each type you use. Breaking API changes show up there first |
| Discord, Slack, Telegram limits | `destinations.py` | Title 256, embed description 4096, Telegram 4096, Slack section 3000. Check the services' docs if posts start getting refused |
| Tracking parameter list | `scheduler.py`, `_TRACKING_PARAM` | If a feed's links carry a new click-tracking parameter that changes every download, add it here |

## 4. Run the checks locally

1. `python3 -m venv venv`
2. `venv/bin/pip install --upgrade -r Docker/requirements.txt pytest`
3. `venv/bin/python -m pytest -q tests`
4. `shellcheck Docker/docker_entrypoint.sh`, if you have shellcheck installed.

GitHub Actions runs the same checks, plus a Docker build and health check, on every push to main and every pull request.

## 5. Data upkeep

Nothing needs pruning by hand. The scheduler trims the seen-article memory, the delivery log (500 entries) and old login throttles on its own.

- `*.bak` and `*.migrated` files pile up in the data folder after upgrades and restores. Once an upgrade has run fine for a few weeks, delete the older ones.
- To look inside the state database: `sqlite3 /data/compose/hotofftheprss/prss_state.db "SELECT COUNT(*) FROM sent;"`. Do this with the container stopped if you plan to change anything.

## 6. When something breaks

| Symptom | Likely cause |
|---|---|
| "Scheduler is not running" | Check the log for a crash loop. The entrypoint restarts the scheduler every 5 seconds and logs each exit |
| "Another scheduler is already using this data directory" | A second copy (another container, a systemd service) points at the same folder. Stop one |
| "Accounts file unreadable" page | `user.json` is damaged. Restore the newest `user.json.pre-*.bak` |
| Feeds can not be saved, red banner about `config.json` | `config.json` is damaged. Restore a backup from the Backup page or the newest `config.json.*.bak` |
| Permission denied errors after an update | `PUID`/`PGID` do not match the data folder's owner and the container could not change it. Fix the owner on the host, or set `PUID=0` |
| A feed posts the same story again | Look at the story's links in the raw feed for a parameter that changes each time and add it to `_TRACKING_PARAM`. Then open an issue with the feed address |
