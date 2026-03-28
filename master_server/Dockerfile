FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 62071

ENV HOST=0.0.0.0 \
    PORT=62071 \
    DB_PATH=/data/proxy_checker.db \
    NODE_TOKEN="" \
    BOOTSTRAP_PROXY_FILES= \
    BOOTSTRAP_PROXY_URLS= \
    BOOTSTRAP_PROXY_URL_FILE=master_server/bootstrap_proxy_urls.txt \
    URL_REFRESH_INTERVAL_SECONDS=300 \
    BOOTSTRAP_MAX_RETRIES=2 \
    POOL_EXPORT_DIR=/data/pool_exports \
    POOL_EXPORT_INTERVAL_SECONDS=300 \
    EXCELLENT_SUCCESS_RATE=0.8 \
    EXCELLENT_MIN_CHECKS=3 \
    LOG_DIR=/data/logs/master \
    LOG_LEVEL=INFO \
    LOG_RETENTION_DAYS=14 \
    UI_CORS_ORIGINS=*

CMD ["sh", "-c", "python -m master_server.main --host ${HOST} --port ${PORT} --db-path ${DB_PATH} --bootstrap-proxy-files \"${BOOTSTRAP_PROXY_FILES}\" --bootstrap-proxy-urls \"${BOOTSTRAP_PROXY_URLS}\" --bootstrap-proxy-url-file \"${BOOTSTRAP_PROXY_URL_FILE}\" --url-refresh-interval-seconds ${URL_REFRESH_INTERVAL_SECONDS} --bootstrap-max-retries ${BOOTSTRAP_MAX_RETRIES} --pool-export-dir \"${POOL_EXPORT_DIR}\" --pool-export-interval-seconds ${POOL_EXPORT_INTERVAL_SECONDS} --excellent-success-rate ${EXCELLENT_SUCCESS_RATE} --excellent-min-checks ${EXCELLENT_MIN_CHECKS} --log-dir \"${LOG_DIR}\" --log-level ${LOG_LEVEL} --log-retention-days ${LOG_RETENTION_DAYS} --ui-cors-origins \"${UI_CORS_ORIGINS}\""]
