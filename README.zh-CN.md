# 分布式代理可用性检测系统

[English Version](README.md)

本系统采用主从架构：

- 主服务器：导入代理、生成任务、分配任务、汇总结果。
- 检测节点：按节点名称领取任务，检测代理可用性并上报详细指标。

## 关键能力

- **节点具名与心跳**：支持自动命名与心跳机制。若不传 node_name，节点默认按 `AA-node-BB` 规则生成（AA 为节点公网 IP 所属国家两位大写码，BB 为该国家在主服务器登记顺序）。
- **自动任务分发**：后台定时向各在线节点分发检测任务，确保每个代理被多个节点检测到。
- **代理池分层**：根据检测结果自动分为三级代理池：
  - excellent：近 7 天内检测成功率达到阈值（默认 >= 80%，且至少 3 次检测）
  - good：有成功记录但未达到 excellent 判定阈值
  - bad：所有节点检测均失败
- **动态管理**：自动清理长期（1天+）不可用的代理，定期重算代理池等级，并自动清理达到失败阈值的代理。
- **多源导入**：支持 JSON、单网页 URL、多网页 URL（并发）、文件导入。
- **启动自动导入**：主服务器启动时可自动从本地代理文件和 URL 列表导入代理。
- **定时 URL 刷新**：主服务器可按固定间隔自动从 URL 重新抓取代理。
- **全池去重**：后台周期性执行代理池去重（规范化 `scheme://host:port`），并自动合并关联任务与检测结果。
- **GitHub 源时效校验**：对于来自 GitHub 仓库的代理源，主服务器会检查仓库最近更新时间；若距今超过 7 天则自动跳过。
- **异常恢复（最小容错）**：主服务器启动与后台周期会自动回收超时 `assigned` 任务（默认 120 秒）；节点请求失败采用指数退避重试，避免重启期间请求风暴。
- **结果明细**：查询代理时返回各节点的最近检测结果，包含延迟、HTTP 状态码等。

## 目录结构

- master_server/：主服务器代码
- worker_node/：检测节点代码
- master_ui/：分离式可视化 UI（独立部署，输入 API 地址即可使用）
- main.py：统一入口

## 安装依赖

```bash
pip install -r requirements.txt
```

## 启动

### Docker（分别打包）

构建主服务器镜像：

```bash
docker build -f master_server/Dockerfile -t proxy-master:latest .
```

构建检测节点镜像：

```bash
docker build -f worker_node/Dockerfile -t proxy-worker:latest .
```

运行主服务器容器：

```bash
docker run -d --name proxy-master -p 62071:62071 -v $(pwd)/data:/data -e NODE_TOKEN=your-token proxy-master:latest
```

运行检测节点容器：

```bash
docker run -d --name proxy-worker -e MASTER_URL=http://host.docker.internal:62071 -e NODE_TOKEN=your-token -e WORKER_COUNT=4 proxy-worker:latest
```

不手动提供 token（自动模式）：

```bash
# 仅启动 master：会自动生成 token 并保存到 data/node_token.txt
docker run -d --name proxy-master -p 62071:62071 -v $(pwd)/data:/data proxy-master:latest

# 启动 worker：从同一挂载目录读取 token 文件
docker run -d --name proxy-worker -v $(pwd)/data:/data -e MASTER_URL=http://host.docker.internal:62071 -e NODE_TOKEN_FILE=/data/node_token.txt -e WORKER_COUNT=4 proxy-worker:latest
```

说明：worker 在未提供 `NODE_TOKEN` 时，会尝试从 `NODE_TOKEN_FILE` 读取，并默认最多等待 30 秒（可用 `NODE_TOKEN_FILE_WAIT_SECONDS` 调整）。

可使用完全拆分的 compose 独立启动（推荐）：

```bash
# 仅启动 master（不会创建 worker）
docker compose -f docker-compose.master.yml up -d --build

# 仅启动 worker（不会创建 master；请确保 MASTER_URL 指向可访问的主服务）
docker compose -f docker-compose.worker.yml up -d --build
```

对应停止命令：

```bash
docker compose -f docker-compose.master.yml down
docker compose -f docker-compose.worker.yml down
```

### 分离式 UI（独立）

```bash
cd master_ui
python -m http.server 5173
```

打开浏览器访问 `http://127.0.0.1:5173`，在页面顶部输入主服务器 API 地址和节点令牌即可可视化调用接口。

如果 UI 与 API 不同源，请启动主服务器时配置 CORS：

```bash
python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --node-token your-token --ui-cors-origins "http://127.0.0.1:5173,http://localhost:5173"
```

### 主服务器

```bash
python -m master_server.main --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --node-token your-token --bootstrap-proxy-files "D:/data/proxies.txt,D:/data/extra.txt" --bootstrap-proxy-urls "https://example.com/proxy1.txt,https://example.com/proxy2.txt" --bootstrap-proxy-url-file "D:/data/proxy_urls.txt" --url-refresh-interval-seconds 300 --bootstrap-max-retries 2 --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --excellent-success-rate 0.8 --excellent-min-checks 3 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14
```

如不传 `--node-token`，master 会自动生成 token 并默认写入 `db-path` 同目录下的 `node_token.txt`。

参数说明：
- `--bootstrap-proxy-files`：启动时导入本地代理文件（多个文件用逗号分隔）。
- `--bootstrap-proxy-urls`：启动时导入并参与定时刷新的 URL 列表（逗号分隔）。
- `--bootstrap-proxy-url-file`：本地 URL 列表文件（每行一个 URL），默认 `master_server/bootstrap_proxy_urls.txt`。
- `--url-refresh-interval-seconds`：定时 URL 刷新间隔，默认 300 秒，最小 30 秒。
- `--bootstrap-max-retries`：自动导入任务生成时的最大重试次数。
- `--pool-export-dir`：代理池可用代理导出目录；默认 `db-path` 同目录下 `pool_exports`。
- `--pool-export-interval-seconds`：代理池导出间隔，默认 300 秒，最小 30 秒。
- `--excellent-success-rate`：excellent 判定成功率阈值，默认 0.8（范围限制 0.5~1.0）。
- `--excellent-min-checks`：excellent 判定最小检测次数，默认 3（范围限制 1~20）。
- `--log-dir`：日志目录；默认 `db-path` 同目录下 `logs/master`。
- `--log-level`：日志级别，默认 `INFO`。
- `--log-retention-days`：日志保留天数，默认 14；超过保留周期的历史日志会自动清理。

默认 URL 检测名单文件：
- `master_server/bootstrap_proxy_urls.txt`
- 已根据 `Proxy-Master/getproxy.py` 进行筛选写入。
- 启动与定时刷新时会自动应用以下规则：
  - 跳过模板化 URL（包含 `{` 或 `}`）
  - 对 GitHub 项目源检查仓库 `pushed_at`，超过 7 天不纳入可用名单
  - 对仅 `ip:port` 的代理：优先根据来源 URL/文件名推断协议类型（如 socks4/socks5/http/https）；若无法推断，则自动按 `http/https/socks4/socks5` 全类型加入

后台会自动启动以下定时任务（每 30 秒一次）：
- 向在线节点分发代理检测任务
- 重算代理池等级
- 清理长期不可用代理
- 按配置间隔从 URL 自动刷新代理
- 执行全池去重（规范化并合并重复代理）
- 按配置间隔将可用代理（status=alive）导出到 `output/` 目录，并按协议分子目录

默认导出结构（以 `--pool-export-dir` 为目录）：
- `output/http/excellent.txt`
- `output/http/good.txt`
- `output/http/bad.txt`
- `output/http/excellent+good.txt`
- `output/http/unknown.txt`
- `output/https/excellent.txt`
- `output/https/good.txt`
- `output/https/bad.txt`
- `output/https/excellent+good.txt`
- `output/https/unknown.txt`
- `output/socks4/excellent.txt`
- `output/socks4/good.txt`
- `output/socks4/bad.txt`
- `output/socks4/excellent+good.txt`
- `output/socks4/unknown.txt`
- `output/socks5/excellent.txt`
- `output/socks5/good.txt`
- `output/socks5/bad.txt`
- `output/socks5/excellent+good.txt`
- `output/socks5/unknown.txt`

### 检测节点

节点启动后会自动向主服务器注册，然后轮询拉取任务并执行检测。

```bash
python -m worker_node.main --master-url http://127.0.0.1:62071 --worker-id worker --worker-count 4 --node-token your-token
```

可选日志参数：
- `--log-dir`：日志目录，默认 `./logs/worker`
- `--log-level`：日志级别，默认 `INFO`
- `--log-retention-days`：日志保留天数，默认 14；超过保留周期的历史日志会自动清理

若不传 `--node-token`，worker 会按顺序尝试：
- 读取环境变量 `NODE_TOKEN`
- 读取文件 `NODE_TOKEN_FILE`（默认等待 30 秒，可用 `NODE_TOKEN_FILE_WAIT_SECONDS` 调整）

说明：
- 默认 `worker_count=4`，会自动启动 `worker-1` 到 `worker-4` 并发检测。
- 可通过 `--worker-count` 调整单节点并发 worker 数量。
- 若不传 `--node-name`，将自动生成 `AA-node-BB`（如 `US-node-01`）。

### 统一入口（可选）

```bash
python main.py master --host 0.0.0.0 --port 62071 --db-path proxy_checker.db --node-token your-token --bootstrap-proxy-files "D:/data/proxies.txt" --bootstrap-proxy-urls "https://example.com/proxy.txt" --pool-export-dir "D:/data/pool_exports" --pool-export-interval-seconds 300 --excellent-success-rate 0.8 --excellent-min-checks 3 --log-dir "D:/data/logs/master" --log-level INFO --log-retention-days 14
python main.py worker --master-url http://127.0.0.1:62071 --worker-id worker --worker-count 4 --node-token your-token --log-dir "D:/data/logs/worker" --log-level INFO --log-retention-days 14
```

## API 文档（JSON 请求/响应示例）

### 1. 健康检查

- 方法路径：GET /health
- 请求体：无
- 响应 200

```json
{
  "status": "ok"
}
```

### 1.1 节点注册

- 方法路径：POST /nodes/register
- 请求头：X-Node-Token: your-token
- 请求体

```json
{
  "node_name": "bj-node-01",
  "worker_id": "worker-a"
}
```

- 响应 200

```json
{
  "status": "ok"
}
```

### 1.1.1 分配默认节点名称（内部接口）

- 方法路径：POST /nodes/allocate-name
- 请求头：X-Node-Token: your-token
- 请求体

```json
{
  "country_code": "US"
}
```

- 响应 200

```json
{
  "node_name": "US-node-01"
}
```

说明：主服务器按国家维度原子分配序号，自动生成格式 `AA-node-BB`。

### 1.2 节点心跳

- 方法路径：POST /nodes/heartbeat
- 请求头：X-Node-Token: your-token
- 请求体

```json
{
  "node_name": "bj-node-01"
}
```

- 响应 200

```json
{
  "status": "ok"
}
```

### 1.4 Worker 心跳（内部接口）

- 方法路径：POST /nodes/worker-heartbeat
- 请求头：X-Node-Token: your-token
- 请求体

```json
{
  "node_name": "bj-node-01",
  "worker_id": "worker-2"
}
```

- 响应 200

```json
{
  "status": "ok"
}
```

### 1.3 查看在线节点列表

- 方法路径：GET /nodes/online?heartbeat_timeout=60
- 请求体：无
- 响应 200

```json
[
  {
    "node_name": "bj-node-01",
    "worker_id": "worker-a",
    "last_heartbeat": "2026-03-27T13:15:00+00:00",
    "registered_at": "2026-03-27T13:00:00+00:00"
  },
  {
    "node_name": "sh-node-02",
    "worker_id": "worker-c",
    "last_heartbeat": "2026-03-27T13:14:55+00:00",
    "registered_at": "2026-03-27T12:50:00+00:00"
  }
]
```

### 2. 直接导入代理（JSON）

- 方法路径：POST /proxies/import
- 请求体

```json
{
  "proxies": [
    "http://1.1.1.1:80",
    "http://2.2.2.2:8080",
    "socks5://3.3.3.3:1080"
  ],
  "max_retries": 2
}
```

- 响应 200

```json
{
  "imported": 3,
  "ignored": 0
}
```

### 3. 从单个网页导入代理

- 方法路径：POST /proxies/import/url
- 请求体

```json
{
  "url": "https://example.com/proxy-list.txt",
  "max_retries": 2
}
```

说明：网页内容按行解析，支持每行一个代理，如 http://1.1.1.1:80。

- 响应 200

```json
{
  "imported": 120,
  "ignored": 5
}
```

### 4. 从多个网页并发导入代理

- 方法路径：POST /proxies/import/urls
- 请求体（会并发抓取多个 URL，自动去重）

```json
{
  "urls": [
    "https://example.com/proxy-list-1.txt",
    "https://example.com/proxy-list-2.txt",
    "https://another-site.com/proxies.txt"
  ],
  "max_retries": 2
}
```

- 响应 200

```json
{
  "imported": 250,
  "ignored": 15
}
```

### 5. 从文件导入代理

- 方法路径：POST /proxies/import/file?max_retries=2
- 请求格式：multipart/form-data，字段名 file
- 文件内容示例（每行一个代理）

```text
http://1.1.1.1:80
http://8.8.8.8:8080
socks5://9.9.9.9:1080
```

- 响应 200

```json
{
  "imported": 60,
  "ignored": 3
}
```

### 6. 查询代理列表（含各节点结果）

- 方法路径：GET /proxies?status=alive&pool_tier=excellent&limit=100&offset=0
- 支持的查询参数：
  - status：unknown, alive, dead（可选）
  - pool_tier：unknown, excellent, good, bad（可选，2.0 新增）
  - limit：每页数量（1-500，默认 100）
  - offset：偏移量（默认 0）
- 请求体：无
- 响应 200

```json
[
  {
    "id": 1,
    "proxy_url": "http://1.1.1.1:80",
    "protocol": "http",
    "status": "alive",
    "pool_tier": "excellent",
    "latency_ms": 245,
    "error": null,
    "last_checked_at": "2026-03-27T13:10:00+00:00",
    "updated_at": "2026-03-27T13:10:00+00:00",
    "node_results": [
      {
        "node_name": "bj-node-01",
        "worker_id": "worker-a",
        "success": true,
        "latency_ms": 245,
        "response_status": 200,
        "error": null,
        "checked_at": "2026-03-27T13:10:00+00:00"
      },
      {
        "node_name": "sh-node-02",
        "worker_id": "worker-c",
        "success": false,
        "latency_ms": 1800,
        "response_status": null,
        "error": "Read timed out",
        "checked_at": "2026-03-27T13:08:22+00:00"
      }
    ]
  }
]
```

### 7. 查询任务列表

- 方法路径：GET /tasks?limit=100&offset=0
- 请求体：无
- 响应 200

```json
[
  {
    "id": 10,
    "task_uuid": "4f8c4121-8d8f-4e36-bfc2-ae31aaf84c31",
    "proxy_id": 1,
    "status": "done",
    "assigned_to": "bj-node-01",
    "assigned_worker_id": "worker-a",
    "attempt": 0,
    "max_retries": 2,
    "created_at": "2026-03-27T13:00:00+00:00",
    "finished_at": "2026-03-27T13:00:01+00:00"
  }
]
```

### 8. 查询统计运营数据

- 方法路径：GET /stats
- 请求体：无
- 响应 200

```json
{
  "proxy_total": 100,
  "proxy_alive": 55,
  "proxy_dead": 45,
  "task_pending": 2,
  "task_assigned": 3,
  "task_done": 90,
  "task_failed": 45
}
```

### 9. 手动重算代理池等级

- 方法路径：POST /pool/recalculate
- 请求体：无
- 响应 200

```json
{
  "status": "ok"
}
```

说明：后台已自动每 30 秒执行一次，无需手动触发。

### 10. 手动清理不可用代理

- 方法路径：POST /pool/cleanup?days=1
- 请求体：无
- 响应 200

```json
{
  "deleted": 5
}
```

说明：清理超过指定天数（默认 1 天）且状态为 "dead" 且池等级为 "bad" 的代理。

### 10.1 手动清理失败次数达到阈值的代理

- 方法路径：POST /pool/cleanup-failed?fail_threshold=5
- 请求体：无
- 响应 200

```json
{
  "deleted": 3
}
```

说明：清理累计失败次数达到阈值且从未成功过的代理。后台默认阈值为 5。

### 11. 手动指派检测任务

- 方法路径：POST /distribution/dispatch
- 请求体：无
- 响应 200

```json
{
  "status": "ok"
}
```

说明：后台已自动每 30 秒执行一次，确保每个代理被所有在线节点检测到。

### 12. 节点拉取任务（内部接口）

- 方法路径：POST /node/pull-task
- 请求头：X-Node-Token: your-token
- 说明：返回的 `task_id` 为任务 UUID（全局唯一），用于结果回传，避免并发下任务标识冲突。
- 请求体

```json
{
  "node_name": "bj-node-01",
  "worker_id": "worker-a"
}
```

- 响应 200（有任务）

```json
{
  "found": true,
  "task": {
    "task_id": "7b0ccf9e-ec57-4b39-bf8e-5ec5464f49e7",
    "proxy_id": 8,
    "proxy_url": "http://1.1.1.1:80",
    "protocol": "http",
    "attempt": 0,
    "max_retries": 2
  }
}
```

- 响应 200（无任务）

```json
{
  "found": false,
  "task": null
}
```

### 13. 节点提交结果（内部接口）

- 方法路径：POST /node/push-result/{task_id}
- 请求头：X-Node-Token: your-token
- 路径参数：`task_id` 为 pull-task 返回的任务 UUID。
- 请求体

```json
{
  "node_name": "bj-node-01",
  "worker_id": "worker-a",
  "success": true,
  "latency_ms": 245,
  "response_status": 200,
  "error": null
}
```

- 响应 200

```json
{
  "status": "ok"
}
```

- 响应 400

```json
{
  "detail": "Task is assigned to another node"
}
```

## 代理格式说明

- 支持协议：http、https、socks4、socks5
- 每行一个代理，推荐格式：

```text
http://1.1.1.1:80
```

## 工作流程示例

1. **启动主服务器和检测节点**
   - 节点自动向主服务器注册，并开始定时心跳

2. **导入代理**
   ```bash
  curl -X POST http://127.0.0.1:62071/proxies/import \
     -H "Content-Type: application/json" \
     -d '{"proxies":["http://1.1.1.1:80","http://2.2.2.2:8080"],"max_retries":2}'
   ```

3. **后台自动分发任务**
   - 每 30 秒，主服务器自动向所有在线节点分发待检测的代理任务
   - 每个代理都会被分配给所有在线节点进行检测

4. **节点执行检测并上报**
   - 节点从主服务器拉取任务，检测代理可用性
   - 上报检测结果（成功/失败、延迟、HTTP 状态码等）

5. **查看结果**
   ```bash
  curl http://127.0.0.1:62071/proxies?status=alive
  curl http://127.0.0.1:62071/stats
   ```

6. **代理池自动更新**
  - 每 30 秒重算每个代理的池等级（excellent/good/bad/unknown）
  - 清理 1 天内一直不可用的代理
  - 清理累计失败次数达到阈值（默认 5 次）的代理
  - 执行全池去重，并自动迁移重复代理关联的任务和检测结果
  - 按配置导出可用代理到 `output/<protocol>/` 下的分层文件

## 鉴权与传输安全

- 节点到主服务器的内部接口统一通过 `X-Node-Token` 进行鉴权，包含：
  - `/nodes/register`
  - `/nodes/heartbeat`
  - `/nodes/worker-heartbeat`
  - `/node/pull-task`
  - `/node/push-result/{task_id}`
- 建议在生产环境启用 HTTPS，避免令牌与检测数据明文传输。
- 建议将 `NODE_API_TOKEN` 设置为高强度随机值，长度不少于 32 字符，并定期轮换。
- 建议通过网关或防火墙限制仅可信节点来源 IP 可访问内部接口。
- 若对安全等级要求更高，可在 HTTPS 基础上增加 mTLS 双向证书认证。
