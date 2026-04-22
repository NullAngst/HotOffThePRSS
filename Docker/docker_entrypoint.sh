#!/bin/bash

# 1. Start the scheduler in the background
# We use '&' to detach it so the script continues.
python3 scheduler.py &

# 2. Start the Web UI with Gunicorn
# exec: Replaces the shell with the gunicorn process
# --bind: Listens on port 5000 from all interfaces
# --workers: 2 workers is standard for small apps
# --threads: 4 threads per worker handles concurrent requests well
# --access-logfile -: Output logs to stdout (so 'docker logs' works)
# main_web:app : Loads the 'app' object from 'main_web.py'
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --access-logfile - --error-logfile - main_web:app
