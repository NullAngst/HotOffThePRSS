FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRSS_DATA_DIR=/data

WORKDIR /app

COPY Docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The application code now lives in the image. Only data is on the volume,
# so rebuilding the image is all an update needs.
COPY main_web.py scheduler.py prss_core.py destinations.py ./
COPY templates ./templates
COPY static ./static
COPY Docker/docker_entrypoint.sh ./docker_entrypoint.sh
# Strip CRLF line endings in case the file was edited on Windows or in a web editor.
RUN sed -i 's/\r$//' docker_entrypoint.sh && chmod +x docker_entrypoint.sh

VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

# Invoke through bash explicitly so the container still starts if the
# shebang line is ever lost.
ENTRYPOINT ["bash", "/app/docker_entrypoint.sh"]
