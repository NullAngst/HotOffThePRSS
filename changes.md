# Changes

2026.11.26 - Backend overhaul / Security patches

* Articles whose guid or link changes on every download (fresh `utm_` tracking parameters, random guids) are no longer posted again on each check. Every article is also remembered by title plus date, and entries without a guid by title plus cleaned-up link.
* A post that times out after the destination received it is no longer retried, which used to post it once per check. Matrix posts use a fixed transaction id per article, so they are still retried safely.
* Articles are recorded as sent before the request goes out and released again on a clean failure. A crash or a failed database write after a post can no longer repeat it on every check.
* Sources are downloaded in full at least every 6 hours. A feed that only answered "304 Not Modified" for a month used to lose its memory and repost its undated articles.
* A source not read for about four weeks (paused or failing) has its articles marked as seen again instead of reposting them.
* A new destination is only set up once every source of the feed loads, so a source that was down at that moment can not post its backlog later.
* Only one scheduler runs per data folder.
* The scheduler no longer retries a feed every 3 seconds when its result can not be saved.
* Feed downloads are cut off after 20 seconds of wall-clock time even when a server sends one byte at a time. Before, such a server could hold a worker for hours.
* Only permanent redirects (301, 308) show a feed as Moved. Temporary 302 and 307 redirects no longer suggest switching addresses.
* Discord posts that are refused for one bad embed field are resent in a plainer form instead of being dropped.
* Telegram messages that are too long are shortened by dropping parts instead of cutting HTML mid-tag, which Telegram refused.
* Slack links containing `|` or `>` no longer break the message.
* Send test gives up on rate limits after a few seconds, so it can not outlive the web request.
* Security: a damaged `user.json` no longer opens first-time setup to anyone. Sign-in is blocked with a clear error page until it is fixed.
* Security: a damaged `config.json` is never overwritten by a feed edit. Changes are refused with an error until it is fixed or restored.
* Security: fixed an open redirect after sign-in (`next=/\evil.example`) and after an expired form (foreign Referer).
* Security: failed sign-ins are also throttled per address across all usernames, which stops password spraying.
* Security: the Docker container runs as `PUID:PGID` (default 1000:1000) instead of root.
* Passwords stored with the old pbkdf2 method are re-hashed with Werkzeug's current default at the next sign-in.
* Entering "inf" or "nan" as an interval shows a form error instead of a server error.
* The default User-Agent follows Chrome's release schedule by date instead of a hardcoded version.
* `/healthz` returns 503 when the scheduler is down, so Docker and Portainer show the container as unhealthy.
* Docker: `docker stop` now stops the scheduler cleanly too (it used to be killed mid-post), and the image rebuilds from the newest Python 3 base and package releases on every deploy.
* CSS and JS links carry a content hash, so browsers load the new files after an update.
* The dashboard updates a feed's paused state live when it changes elsewhere.
* Removed `convert_config.sh`; the app has converted old configs on its own for several versions.
* Added a test suite, GitHub Actions checks, `maintenance.md` and this file.

Reasons for changes: an audit for duplicate posts, lockups and security holes. Most of the duplicate cases were reproduced against a local feed and destination before being fixed, and the test suite keeps them fixed.
