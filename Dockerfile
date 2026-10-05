FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ghostsignal ./ghostsignal
COPY tools ./tools
# Database lives on the Railway volume mounted at /data so it survives redeploys.
ENV GHOSTSIGNAL_DB=/data/ghostsignal.db \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
CMD ["python", "-m", "ghostsignal.cli", "serve"]
