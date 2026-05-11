FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 \
    GEMINI_DATA_DIR=/app/data \
    GEMINI_DATABASE_PATH=/app/data/app.db \
    GEMINI_ACCOUNTS_FILE=/app/data/accounts.json \
    GEMINI_AUTH_HEADLESS=false \
    HOST=0.0.0.0 \
    PORT=7860

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[server]" \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends x11vnc websockify novnc \
    && rm -rf /var/lib/apt/lists/*

VOLUME ["/app/data"]
EXPOSE 7860 6080

CMD ["gemini-webapi-server"]
