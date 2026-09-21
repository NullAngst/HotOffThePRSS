#!/bin/bash
set -u

# Run migrations once, up front, so the scheduler and every gunicorn worker
# start against an up-to-date data directory.
python3 -c "import prss_core; prss_core.initialize()" || exit 1

# Keep the scheduler alive. Previously a crash left the container running
# with nothing posting and no sign of it outside the logs.
(
  while true; do
    python3 scheduler.py
    echo "scheduler exited with status $?; restarting in 5 seconds" >&2
    sleep 5
  done
) &

# Listen on IPv6 and IPv4. A 0.0.0.0-only bind refuses connections that a
# reverse proxy makes to the container's IPv6 address, which shows up as
# intermittent 502s. Falls back to IPv4 only if IPv6 is disabled in the
# container. PRSS_BIND overrides the choice entirely.
BIND="${PRSS_BIND:-}"
if [ -z "$BIND" ]; then
  if python3 -c "import socket; s=socket.socket(socket.AF_INET6); s.bind(('::', 0))" 2>/dev/null; then
    BIND="[::]:5000"
  else
    BIND="0.0.0.0:5000"
  fi
fi

# Keep idle connections open longer than a reverse proxy's upstream
# keepalive (nginx uses 60s), so the proxy never reuses a connection
# gunicorn has just closed. Gunicorn's own default is 2 seconds.
exec gunicorn --bind "$BIND" --workers 2 --threads 4 --timeout 60 \
  --keep-alive "${PRSS_KEEPALIVE:-75}" \
  --access-logfile - --error-logfile - main_web:app
