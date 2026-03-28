# Distributed Proxy Availability Checking System

[中文版本 (Simplified Chinese)](README.zh-CN.md)

This project uses a master-worker architecture for large-scale proxy validation.

- Master server: imports proxies, creates tasks, dispatches tasks, and aggregates check results.
- Worker nodes: pull tasks by node name, validate proxy availability, and report detailed metrics.

## Key Capabilities

- Named nodes and heartbeat support:
  - If `node_name` is not provided, a name is generated as `AA-node-BB`.
  - `AA` is the 2-letter uppercase country code derived from node public IP.
  - `BB` is the registration sequence for that country.
- Automatic task distribution:
  - Background schedulers continuously distribute tasks to online nodes.
  - Each proxy can be checked by multiple nodes.
- Tiered proxy pools:
  - `excellent`: success rate in the last 7 days reaches threshold (default `>= 80%`) and at least 3 checks.
  - `good`: has successful checks but does not meet `excellent` threshold.
  - `bad`: all checks failed.
- Dynamic lifecycle management:
  - Cleans up proxies unavailable for a long time (1 day+).
  - Recomputes pool tiers periodically.
  - Removes proxies that reach failure thresholds.
- Multi-source imports:
  - JSON payload, single URL, multiple URLs (concurrent), and file upload.
- Startup bootstrap import:
  - Master can auto-import proxies from local files and URL lists on startup.
- Scheduled URL refresh:
  - Master can periodically refresh proxies from configured URLs.
- Global deduplication:
  - Periodic normalization by `scheme://host:port` with task/result merge.
- GitHub source freshness check:
  - For GitHub-based sources, repositories not updated for over 7 days are skipped.
- Resilience and recovery:
  - Timed-out `assigned` tasks are reclaimed automatically (default 120 seconds).
  - Worker retries use exponential backoff to avoid restart storms.
- Rich result details:
  - Query endpoints return latest per-node results including latency and HTTP status code.

## Project Structure

- `master_server/`: master server implementation
- `worker_node/`: worker node implementation
- `master_ui/`: standalone web UI (separate deployment, configurable API endpoint)
- `main.py`: unified entry point

## Install Dependencies

```bash
pip install -r requirements.txt
```

## Run with Docker (Separate Images)

Build master image:

```bash
docker build -f master_server/Dockerfile -t proxy-master:latest .
```

Build worker image:

```bash
docker build -f worker_node/Dockerfile -t proxy-worker:latest .
```

Run master container:

```bash
docker run -d --name proxy-master -p 62071:62071 -v $(pwd)/data:/data -e NODE_TOKEN=your-token proxy-master:latest
```

Run worker container:

```bash
docker run -d --name proxy-worker -e MASTER_URL=http://host.docker.internal:62071 -e NODE_TOKEN=your-token -e WORKER_COUNT=4 -e TARGET_URL=https://npmjs.org/cdn-cgi/trace proxy-worker:latest
```

Run with split compose files (recommended):

```bash
# Master only
docker compose -f docker-compose.master.yml up -d --build

# Worker only (MASTER_URL must point to reachable master API)
docker compose -f docker-compose.worker.yml up -d --build
```

Stop compose stacks:

```bash
docker compose -f docker-compose.master.yml down
docker compose -f docker-compose.worker.yml down
```

## Standalone UI

```bash
cd master_ui
python -m http.server 5173
```

Open `http://127.0.0.1:5173` and enter:
- master API base URL
- node token

If UI and API are cross-origin, enable CORS on master startup:

```bash
NODE_TOKEN=your-token python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --ui-cors-origins "http://127.0.0.1:5173,http://localhost:5173"
```

## Run Master Server (Python)

```bash
NODE_TOKEN=your-token python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --bootstrap-proxy-files "D:/data/proxies.txt,D:/data/extra.txt" --bootstrap-proxy-urls "https://example.com/proxy1.txt,https://example.com/proxy2.txt" --bootstrap-proxy-url-file "D:/data/proxy_urls.txt" --url-refresh-interval-seconds 300 --bootstrap-max-retries 2 --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --excellent-success-rate 0.8 --excellent-min-checks 3 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14
```

Master reads token from environment variable `NODE_TOKEN`.

Important options:
- `--bootstrap-proxy-files`: comma-separated local proxy files to import at startup.
- `--bootstrap-proxy-urls`: comma-separated URLs imported at startup and included in periodic refresh.
- `--bootstrap-proxy-url-file`: local URL list file (one URL per line), default `master_server/bootstrap_proxy_urls.txt`.
- `--url-refresh-interval-seconds`: URL refresh interval, default `300`, minimum `30`.
- `--bootstrap-max-retries`: max retries while creating bootstrap tasks.
- `--pool-export-dir`: output directory for exported proxy pool files.
- `--pool-export-interval-seconds`: export interval, default `300`, minimum `30`.
- `--excellent-success-rate`: success threshold for `excellent`, default `0.8` (range `0.5` to `1.0`).
- `--excellent-min-checks`: min checks for `excellent`, default `3` (range `1` to `20`).
- `--log-dir`: log directory.
- `--log-level`: log level, default `INFO`.
- `--log-retention-days`: log retention days, default `14`.

Default URL source file:
- `master_server/bootstrap_proxy_urls.txt`

Auto filters for startup and periodic refresh:
- Skip template URLs containing `{` or `}`.
- For GitHub sources, skip repositories with `pushed_at` older than 7 days.
- For raw `ip:port` entries, protocol is inferred from source URL/file name when possible (`socks4`, `socks5`, `http`, `https`); otherwise all protocol variants are generated.

Background jobs (every 30 seconds):
- Dispatch proxy check tasks to online nodes.
- Recompute proxy pool tiers.
- Clean long-term unavailable proxies.
- Refresh URLs at configured intervals.
- Run global deduplication and merge duplicates.
- Export alive proxies (`status=alive`) by protocol under output directories.

## Run Worker Node (Python)

After startup, a worker auto-registers on master and continuously pulls check tasks.

```bash
python -m worker_node.main --master-url http://127.0.0.1:62071 --worker-id worker --worker-count 4 --node-token your-token --target-url https://npmjs.org/cdn-cgi/trace
```

Optional logging options:
- `--log-dir`: log directory, default `./logs/worker`
- `--log-level`: log level, default `INFO`
- `--log-retention-days`: log retention days, default `14`

If `--node-token` is omitted, worker tries in order:
- environment variable `NODE_TOKEN`
- file path in `NODE_TOKEN_FILE` (wait duration controlled by `NODE_TOKEN_FILE_WAIT_SECONDS`, default 30 seconds)

Notes:
- Default `worker_count` is 4, starting concurrent workers from `worker-1` to `worker-4`.
- You can tune concurrency with `--worker-count`.
- If `--node-name` is omitted, a name like `US-node-01` is auto-generated.
- Default probe target is `https://npmjs.org/cdn-cgi/trace`; worker extracts `loc=XX` and reports country code.

## Unified Entry Point (Optional)

```bash
NODE_TOKEN=your-token python main.py master --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --bootstrap-proxy-files "D:/data/proxies.txt" --bootstrap-proxy-urls "https://example.com/proxy.txt" --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --excellent-success-rate 0.8 --excellent-min-checks 3 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14

python main.py worker --master-url http://127.0.0.1:62071 --worker-id worker --worker-count 4 --node-token your-token --target-url https://npmjs.org/cdn-cgi/trace --log-dir "D:/data/logs/worker" --log-level INFO --log-retention-days 14
```

## API Overview

Main endpoint groups:
- Health check
- Node registration and heartbeat
- Worker heartbeat
- Online node query
- Proxy import from JSON, single URL, multiple URLs, and file upload
- Proxy query with detailed per-node check records

UI overview includes `Alive Rate = proxy_alive / (proxy_alive + proxy_dead)`.

For concrete request/response examples, check server routes and schemas in:
- `master_server/api.py`
- `master_server/schemas.py`

## Chinese Version

The previous Chinese README is preserved as:
- `README.zh-CN.md`
