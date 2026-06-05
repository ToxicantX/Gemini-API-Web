FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 \
    GEMINI_DATA_DIR=/app/data \
    GEMINI_DATABASE_PATH=/app/data/app.db \
    GEMINI_ACCOUNTS_FILE=/app/data/accounts.json \
    HOST=0.0.0.0 \
    PORT=7860

WORKDIR /app

RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries

RUN pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir playwright \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends x11vnc websockify novnc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir ".[server]"

VOLUME ["/app/data"]
EXPOSE 7860 6080

# 服务器部署时让 Docker 能直接判断 API 进程是否真正可用，而不是只看容器进程是否还在。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port=os.getenv('PORT','7860'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read(); sys.exit(0)"

CMD ["gemini-webapi-server"]
