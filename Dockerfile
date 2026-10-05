FROM python:3.12-slim-bookworm
ARG VERSION=0.9.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="GDeploy" \
    org.opencontainers.image.description="OS and application VM deployment for standalone ESXi" \
    org.opencontainers.image.source="https://github.com/DasFunfZigste/GDeploy" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${REVISION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends xorriso openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home gdeploy \
    && mkdir -p /data /media && chown gdeploy:gdeploy /data
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY gdeploy ./gdeploy
COPY scripts/configure.py ./scripts/configure.py
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
CMD ["uvicorn", "gdeploy.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
