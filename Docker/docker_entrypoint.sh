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

exec gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 60 \
  --access-logfile - --error-logfile - main_web:app
