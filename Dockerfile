# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY config.py costs.py main.py pipeline.py pyproject.toml ./
COPY core ./core
COPY db ./db
COPY agents ./agents
COPY sources ./sources
COPY rag ./rag
COPY observability ./observability
COPY interfaces ./interfaces
COPY scripts ./scripts
COPY templates ./templates
COPY content ./content
COPY data ./data

# Run as a non-root user (DevSecOps hardening).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Default: serve the human approval dashboard. The daily discovery run is a
# separate CronJob (see k8s/cronjob.yaml) that overrides the command.
CMD ["python", "main.py", "dashboard", "--host", "0.0.0.0", "--port", "8000"]
