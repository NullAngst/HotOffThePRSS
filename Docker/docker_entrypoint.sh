#!/usr/bin/env bash
# docker_entrypoint.sh
# Starts the scheduler and the web UI in one container. Hands the data folder
# to PUID:PGID and runs both as that user, restarts the scheduler if it
# crashes, and stops both cleanly on docker stop.
set -euo pipefail

DATA="${PRSS_DATA_DIR:-/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

missing=""
for cmd in python3 gunicorn find chown id mkdir sleep; do
  command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
done
if [ -n "$missing" ]; then
  log "missing required commands:$missing"
  exit 1
fi

case "$PUID:$PGID" in
  *[!0-9:]* | :* | *:) log "PUID and PGID must be numbers (got '$PUID' and '$PGID')"; exit 1 ;;
esac

# Started as root: give the data folder to PUID:PGID, then run this script
# again as that user. Only files with the wrong owner are touched, so this
# is quick after the first start. PUID=0 keeps everything running as root.
if [ "$(id -u)" -eq 0 ] && [ "$PUID" != "0" ]; then
  mkdir -p -- "$DATA"
  if ! find "$DATA" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} + 2>/dev/null; then
    log "could not change the owner of everything in $DATA"
  fi
  if [ -n "$(find "$DATA" -prune -user "$PUID")" ]; then
    exec python3 -c '
import os, sys
uid, gid = int(sys.argv[1]), int(sys.argv[2])
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
os.environ["HOME"] = "/tmp"
os.execvp(sys.argv[3], sys.argv[3:])
' "$PUID" "$PGID" bash "$0" "$@"
  fi
  log "$DATA can not be given to $PUID:$PGID (read-only or network share?); running as root instead"
fi

cd /app || exit 1

# Run migrations once, up front, so the scheduler and every gunicorn worker
# start against an up-to-date data directory.
python3 -c "import prss_core; prss_core.initialize()"

scheduler_loop() {
  local child="" stopping=0 status
  trap 'stopping=1; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; fi' TERM
  while [ "$stopping" -eq 0 ]; do
    python3 scheduler.py &
    child=$!
    status=0
    wait "$child" || status=$?
    if [ "$stopping" -eq 1 ]; then
      # wait returned early because of the signal; let the scheduler finish.
      wait "$child" 2>/dev/null || true
      break
    fi
    child=""
    log "scheduler exited with status $status; restarting in 5 seconds"
    sleep 5 &
    wait $! || true
  done
}

# Listen on IPv6 and IPv4. A 0.0.0.0-only bind refuses connections that a
# reverse proxy makes to the container's IPv6 address, which shows up as
# intermittent 502s. Falls back to IPv4 when IPv6 is disabled in the
# container. PRSS_BIND overrides the choice entirely.
BIND="${PRSS_BIND:-}"
if [ -z "$BIND" ]; then
  if python3 -c "import socket; s=socket.socket(socket.AF_INET6); s.bind(('::', 0))" 2>/dev/null; then
    BIND="[::]:5000"
  else
    BIND="0.0.0.0:5000"
  fi
fi

scheduler_loop &
sched_pid=$!

# Keep idle connections open longer than a reverse proxy's upstream
# keepalive (nginx uses 60s), so the proxy never reuses a connection
# gunicorn has just closed.
gunicorn --bind "$BIND" --workers 2 --threads 4 --timeout 60 --graceful-timeout 20 \
  --keep-alive "${PRSS_KEEPALIVE:-75}" --worker-tmp-dir /dev/shm \
  --access-logfile - --error-logfile - main_web:app &
web_pid=$!

signalled=0
shutdown() {
  kill -TERM "$web_pid" "$sched_pid" 2>/dev/null || true
}
trap 'signalled=1; shutdown' TERM INT

# Whichever exits first (or a stop signal) brings the other one down too.
status=0
wait -n || status=$?
shutdown
wait "$web_pid" 2>/dev/null || true
wait "$sched_pid" 2>/dev/null || true
if [ "$signalled" -eq 1 ]; then
  exit 0
fi
exit "$status"
