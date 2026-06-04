# One image serves both workloads (UI service + batch job); the Cloud Run command
# selects which (`pdf-anonymise serve` vs `pdf-anonymise batch`).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package (with its deps), then drop to a non-root user for runtime.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[gcp]" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && chown -R app /app
USER app

# Cloud Run injects PORT; the app defaults to 8080.
ENV PORT=8080
EXPOSE 8080

# Liveness for local/docker runs (Cloud Run uses its own probes).
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8080')+'/')" || exit 1

# Default to the UI; the batch job overrides the command to `pdf-anonymise batch`.
ENTRYPOINT ["pdf-anonymise"]
CMD ["serve"]
