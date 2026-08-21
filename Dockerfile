# Excel Formula Generator — backend container
#
# Build context is the PROJECT ROOT (the folder containing formula_engine/).
# The image contains Python + dependencies; no .venv exists on your laptop.
# The analytics database lives on a Docker named volume at /data, deliberately
# OUTSIDE the project folder so OneDrive never touches an open SQLite file.

# Python 3.12 is required, not optional: formula_builder.py uses backslashes
# inside f-string expressions, which is a SyntaxError before 3.12 (PEP 701).
FROM python:3.12-slim

# Fail fast and log straight to stdout (so `docker compose logs` shows everything).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: this layer is cached and only rebuilt
# when requirements.txt changes, so editing Python code rebuilds in ~1 second.
COPY formula_engine/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY formula_engine/ /app/

# Run as a non-root user. If the app is ever compromised, the attacker lands as
# an unprivileged user, not root. /data is created and owned here so that Docker
# initialises the named volume with the right ownership on first run.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

ENV DB_PATH=/data/analytics.db

EXPOSE 8000

# Container-level health check — `docker compose ps` will show healthy/unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
