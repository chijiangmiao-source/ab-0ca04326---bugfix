FROM python:3.11-slim-bookworm

# verify 服务的前端 JS 构建检查需要 node；app 服务本身只用 Python 标准库
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_FILE=/data/state.json \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=3s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "app.server"]
