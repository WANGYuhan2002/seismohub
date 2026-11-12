# SeismoHub container image.
#
#   docker compose up -d          # recommended, see docker-compose.yml
#
# or by hand:
#   docker build -t seismohub .
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
#     -v "$PWD/data:/app/data" -v "$PWD/downloads:/app/downloads" -v "$PWD/paper_bundles:/app/paper_bundles" \
#     seismohub
#
# The app has no login: keep the port bound to 127.0.0.1 or put an authenticating reverse proxy in front.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SEISMO_OPEN_BROWSER=0 \
    SEISMO_PORT=8000 \
    SEISMO_DB_PATH=/app/data/seismo_hub.db

# curl for the health check; libstdc++ for the PyMuPDF wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libstdc++6 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so they cache across code changes. The test-only packages are skipped.
COPY requirements.txt ./
RUN grep -v -i -E '^(pytest|responses|freezegun)==' requirements.txt > /tmp/requirements-runtime.txt \
 && pip install -r /tmp/requirements-runtime.txt

COPY . ./

# Library data lives in volumes: SQLite DB, managed PDFs, full-text bundles.
RUN mkdir -p /app/data /app/downloads /app/paper_bundles \
 && useradd --create-home --uid 1000 seismo \
 && chown -R seismo:seismo /app
USER seismo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent "http://localhost:${SEISMO_PORT}/api/health" > /dev/null || exit 1

# 0.0.0.0 inside the container; docker-compose publishes it on 127.0.0.1 of the host only.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${SEISMO_PORT}"]
