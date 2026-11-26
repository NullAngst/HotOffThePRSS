# Tracks the newest Python 3 release on purpose. docker-compose.yml rebuilds
# without cache and pulls this image on every deploy, so each redeploy picks
# up current Python, Debian security fixes and Python packages.
FROM python:3-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PRSS_DATA_DIR=/data \
    PUID=1000 \
    PGID=1000

WORKDIR /app

COPY Docker/requirements.txt .
RUN pip install --upgrade pip && pip install --upgrade -r requirements.txt

# The application code lives in the image. Only data is on the volume, so
# rebuilding the image is all an update needs.
COPY main_web.py scheduler.py prss_core.py destinations.py ./
COPY templates ./templates
COPY static ./static
# Strip CRLF line endings in case the file was edited on Windows or in a
# web editor; bash refuses to run a script with them.
COPY Docker/docker_entrypoint.sh /tmp/docker_entrypoint.sh
RUN tr -d '\r' < /tmp/docker_entrypoint.sh > /app/docker_entrypoint.sh \
    && rm /tmp/docker_entrypoint.sh \
    && chmod 755 /app/docker_entrypoint.sh

VOLUME /data
EXPOSE 5000

# Healthy only while both the web UI and the scheduler are running.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

# Invoked through bash so the container still starts if the executable bit
# is lost (GitHub web uploads drop it).
ENTRYPOINT ["bash", "/app/docker_entrypoint.sh"]
