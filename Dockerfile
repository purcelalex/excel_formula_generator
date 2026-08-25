# Excel Formula Generator — backend container
#
# Build context is the PROJECT ROOT (the folder containing backend/).
# Python and every dependency live in the image, so there is no .venv on your
# laptop. The analytics database lives on a Docker named volume at /data —
# outside the project folder, where no file-sync tool can corrupt it mid-write.
#
# Python 3.12 or newer is required.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies in their own layer so that editing Python code rebuilds in about
# a second instead of reinstalling everything.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/

# Run as an unprivileged user. /data is created and owned here so Docker
# initialises the named volume with the right ownership on first run.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

ENV DB_PATH=/data/analytics.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
