# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
# psycopg2-binary lets the dashboard use a Postgres COPILOT_DATABASE_URL (Neon).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt psycopg2-binary

# Copy application source.
COPY config.py costs.py voice.py runlog.py analytics.py main.py pipeline.py pyproject.toml ./
COPY core ./core
COPY db ./db
COPY agents ./agents
COPY sources ./sources
COPY rag ./rag
COPY observability ./observability
COPY interfaces ./interfaces
COPY outreach ./outreach
COPY reply ./reply
COPY followup ./followup
COPY optimizer ./optimizer
COPY scripts ./scripts
COPY templates ./templates
COPY content ./content
COPY data ./data

# Run as a non-root user (DevSecOps hardening).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Health check honors $PORT (Render/Cloud Run inject it; defaults to 8000 locally).
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,os,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz').status==200 else 1)"

# Default: serve the dashboard. Shell form so ${PORT} (set by Render/Cloud Run) is
# honored — those platforms route to the port they assign, not a fixed 8000.
CMD ["sh", "-c", "python main.py dashboard --host 0.0.0.0 --port ${PORT:-8000}"]
