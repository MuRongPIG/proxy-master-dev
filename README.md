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
  - `excellent`: all 3 node checks in the batch succeed.
  - `good`: 1 or 2 node checks in the batch succeed.
  - `bad`: all 3 node checks in the batch fail.
- Two-pass + blacklist flow:
  - First pass traverses the full queue from head to tail.
  - Second pass checks `bad` first, then `good`.
  - Proxies still `bad` after second pass are moved into blacklist and removed from the main pool.
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
NODE_TOKEN=your-token python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --bootstrap-proxy-files "D:/data/proxies.txt,D:/data/extra.txt" --bootstrap-proxy-urls "https://example.com/proxy1.txt,https://example.com/proxy2.txt" --bootstrap-proxy-url-file "D:/data/proxy_urls.txt" --url-refresh-interval-seconds 300 --bootstrap-max-retries 2 --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14
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
- `--log-dir`: log directory.
- `--log-level`: log level, default `INFO`.
- `--log-retention-days`: log retention days, default `14`.

Default URL source file:
- `master_server/bootstrap_proxy_urls.txt`

Auto filters for startup and periodic refresh:
- Skip template URLs containing `{` or `}`.
- For GitHub sources, skip repositories with `pushed_at` older than 7 days.
- For raw `ip:port` entries, protocol is inferred from source URL/file name when possible (`socks4`, `socks5`, `http`, `https`); otherwise all protocol variants are generated.

Background jobs (every 7200 seconds):
- Dispatch tasks using two-pass flow (primary full traversal, then secondary bad/good).
- Add second-pass bad proxies into blacklist and reject future imports from URL/file feeds.
- Periodically monitor only `excellent` and `good` pools.
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

Worker reads token from environment variable `NODE_TOKEN`.

Notes:
- Default `worker_count` is 4, starting concurrent workers from `worker-1` to `worker-4`.
- You can tune concurrency with `--worker-count`.
- If `--node-name` is omitted, a name like `US-node-01` is auto-generated.
- Default probe target is `https://npmjs.org/cdn-cgi/trace`; worker extracts `loc=XX` and reports country code.

## Unified Entry Point (Optional)

```bash
NODE_TOKEN=your-token python main.py master --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --bootstrap-proxy-files "D:/data/proxies.txt" --bootstrap-proxy-urls "https://example.com/proxy.txt" --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14

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
- Pool-tier trend analytics for all checked proxies: `GET /analytics/pool-tier-trend?days=14`
- Alive proxy protocol distribution analytics: `GET /analytics/alive-protocol-distribution`

UI overview includes `Alive Rate = proxy_alive / (proxy_alive + proxy_dead)`.
UI charts now use analytics endpoints for full-range data (not affected by proxy list pagination/filtering).

For concrete request/response examples, check server routes and schemas in:
- `master_server/api.py`
- `master_server/schemas.py`

## Chinese Version

The previous Chinese README is preserved as:
- `README.zh-CN.md`
