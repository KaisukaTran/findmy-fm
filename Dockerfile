FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

FROM python:3.12-slim
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY . .

# Create app user and pre-create data dir with correct ownership.
# The data/ dir is also a Docker volume mount point — we set permissions
# here so the volume is owned by uid 1001 on first container start.
RUN mkdir -p /app/data /app/data/uploads && \
    chmod +x scripts/migrate.sh scripts/start_prod.sh && \
    addgroup --gid 1001 app && \
    adduser --uid 1001 --gid 1001 --no-create-home --disabled-password app && \
    chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["sh", "scripts/start_prod.sh"]
