FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MASTER_URL=http://master:62071 \
    NODE_TOKEN=change-this-token \
    NODE_NAME= \
    WORKER_ID=worker \
    WORKER_COUNT=4 \
    INTERVAL=2 \
    TIMEOUT=8 \
    HEARTBEAT_INTERVAL=15 \
    TARGET_URL=https://httpbin.org/ip \
    LOG_DIR=/data/logs/worker \
    LOG_LEVEL=INFO \
    LOG_RETENTION_DAYS=14

CMD ["sh", "-c", "if [ -n \"${NODE_NAME}\" ]; then EXTRA_NODE_NAME=\"--node-name ${NODE_NAME}\"; else EXTRA_NODE_NAME=\"\"; fi; python -m worker_node.main --master-url ${MASTER_URL} --node-token ${NODE_TOKEN} --worker-id ${WORKER_ID} --worker-count ${WORKER_COUNT} --interval ${INTERVAL} --timeout ${TIMEOUT} --heartbeat-interval ${HEARTBEAT_INTERVAL} --target-url ${TARGET_URL} --log-dir \"${LOG_DIR}\" --log-level ${LOG_LEVEL} --log-retention-days ${LOG_RETENTION_DAYS} ${EXTRA_NODE_NAME}"]
