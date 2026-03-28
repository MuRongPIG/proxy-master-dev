import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from master_server import database
from master_server.schemas import (
    AliveProtocolDistributionResponse,
    AllocateNodeNameRequest,
    AllocateNodeNameResponse,
    HeartbeatRequest,
    ImportFromUrlRequest,
    ImportFromUrlsRequest,
    ImportProxiesRequest,
    ImportProxiesResponse,
    NodeInfoView,
    PoolTierTrendPoint,
    ProxyView,
    ProtocolDistributionItem,
    PullTaskRequest,
    PullTaskResponse,
    PushResultRequest,
    RegisterNodeRequest,
    StatsResponse,
    TaskPayload,
    TaskView,
    WorkerHeartbeatRequest,
)

app = FastAPI(title="分布式代理检测主服务器", version="2.0.0")
logger = logging.getLogger(__name__)

_GITHUB_REPO_FRESHNESS_CACHE: dict[str, tuple[bool, float]] = {}


def _cors_origins() -> list[str]:
    raw = os.getenv("MASTER_UI_CORS_ORIGINS", "*")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    return origins or ["*"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _split_csv_env(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _extract_github_repo(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host == "raw.githubusercontent.com" and len(parts) >= 2:
        return parts[0], parts[1]
    if host == "github.com" and len(parts) >= 2:
        return parts[0], parts[1]
    return None


def _is_github_repo_recent(url: str, max_age_days: int = 7) -> bool:
    repo = _extract_github_repo(url)
    if not repo:
        return True

    owner, name = repo
    cache_key = f"{owner}/{name}".lower()
    now_ts = time.time()
    cached = _GITHUB_REPO_FRESHNESS_CACHE.get(cache_key)
    if cached and now_ts - cached[1] < 3600:
        return cached[0]

    api = f"https://api.github.com/repos/{owner}/{name}"
    try:
        resp = requests.get(api, timeout=10, headers={"User-Agent": "proxy-master"})
        resp.raise_for_status()
        payload = resp.json()
        pushed_at = payload.get("pushed_at")
        if not pushed_at:
            _GITHUB_REPO_FRESHNESS_CACHE[cache_key] = (False, now_ts)
            return False
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        ok = pushed >= cutoff
        _GITHUB_REPO_FRESHNESS_CACHE[cache_key] = (ok, now_ts)
        return ok
    except requests.RequestException:
        _GITHUB_REPO_FRESHNESS_CACHE[cache_key] = (False, now_ts)
        return False


def _filter_bootstrap_urls(urls: list[str]) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = raw.strip()
        if not url or url in seen:
            continue
        if "{" in url or "}" in url:
            continue
        if not _is_github_repo_recent(url, max_age_days=7):
            continue
        seen.add(url)
        filtered.append(url)
    return filtered


def _bootstrap_max_retries() -> int:
    raw = os.getenv("MASTER_BOOTSTRAP_MAX_RETRIES", "2")
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return 2


def _url_refresh_interval_seconds() -> int:
    raw = os.getenv("MASTER_URL_REFRESH_INTERVAL_SECONDS", "300")
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def _pool_export_interval_seconds() -> int:
    raw = os.getenv("MASTER_POOL_EXPORT_INTERVAL_SECONDS", "300")
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def _task_retention_days() -> int:
    raw = os.getenv("MASTER_TASK_RETENTION_DAYS", "14")
    try:
        value = int(raw)
    except ValueError:
        return 14
    return max(0, min(3650, value))


def _task_cleanup_interval_seconds() -> int:
    raw = os.getenv("MASTER_TASK_CLEANUP_INTERVAL_SECONDS", "600")
    try:
        return max(30, int(raw))
    except ValueError:
        return 600


def _pool_export_dir() -> str:
    export_dir = os.getenv("MASTER_POOL_EXPORT_DIR", "").strip()
    if export_dir:
        return export_dir
    db_path = os.getenv("MASTER_DB_PATH", "proxy_checker.db")
    db_dir = os.path.dirname(db_path) or "."
    return os.path.join(db_dir, "pool_exports")


def _export_pool_once() -> None:
    exported = database.export_alive_proxies_by_tier(export_dir=_pool_export_dir())
    logger.info("代理池导出完成: %s", exported)


def _read_text_file_lines(file_path: str) -> list[str]:
    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            return [line.strip() for line in fp.readlines() if line.strip()]
    except OSError:
        return []


def _get_bootstrap_proxy_urls() -> list[str]:
    urls = _split_csv_env(os.getenv("MASTER_BOOTSTRAP_PROXY_URLS"))
    url_file = os.getenv("MASTER_BOOTSTRAP_PROXY_URL_FILE", "").strip()
    if url_file:
        urls.extend(_read_text_file_lines(url_file))
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return _filter_bootstrap_urls(deduped)


def _load_bootstrap_proxies_from_files() -> list[str]:
    files = _split_csv_env(os.getenv("MASTER_BOOTSTRAP_PROXY_FILES"))
    all_proxies: list[str] = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as fp:
                all_proxies.extend(database.extract_proxies_from_text(fp.read(), source_hint=file_path))
        except OSError:
            continue
    return all_proxies


def _fetch_proxies_from_urls(urls: list[str]) -> list[str]:
    proxies: list[str] = []
    seen: set[str] = set()
    for url in urls:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            fetched = database.extract_proxies_from_text(response.text, source_hint=url)
            for proxy_url in fetched:
                if proxy_url in seen:
                    continue
                seen.add(proxy_url)
                proxies.append(proxy_url)
        except requests.RequestException:
            continue
    return proxies


def _bootstrap_import_once() -> None:
    max_retries = _bootstrap_max_retries()
    proxies_from_files = _load_bootstrap_proxies_from_files()
    if proxies_from_files:
        database.import_proxies(proxies_from_files, max_retries=max_retries)

    urls = _get_bootstrap_proxy_urls()
    if urls:
        proxies_from_urls = _fetch_proxies_from_urls(urls)
        if proxies_from_urls:
            database.import_proxies(proxies_from_urls, max_retries=max_retries)

    database.deduplicate_proxy_pool()


def _refresh_proxy_urls_once() -> None:
    urls = _get_bootstrap_proxy_urls()
    if not urls:
        return
    proxies = _fetch_proxies_from_urls(urls)
    if not proxies:
        return
    database.import_proxies(proxies, max_retries=_bootstrap_max_retries())
    database.deduplicate_proxy_pool()


def _expected_token() -> str:
    return os.getenv("NODE_API_TOKEN", "change-this-token")


def verify_node_token(x_node_token: str = Header(default="")) -> None:
    if x_node_token != _expected_token():
        raise HTTPException(status_code=401, detail="节点令牌无效")


@app.on_event("startup")
def on_startup() -> None:
    logger.info("master 启动中")
    database.init_db()
    database.reclaim_stale_assigned_tasks(timeout_seconds=120)
    _bootstrap_import_once()
    _export_pool_once()
    _start_background_tasks()
    logger.info("master 启动完成")


def _background_task_loop() -> None:
    """后台循环：定时执行任务分配、代理池评级、过期代理清理。"""
    last_url_refresh = 0.0
    last_pool_export = 0.0
    last_task_cleanup = 0.0
    url_refresh_interval = _url_refresh_interval_seconds()
    pool_export_interval = _pool_export_interval_seconds()
    task_cleanup_interval = _task_cleanup_interval_seconds()
    task_retention_days = _task_retention_days()
    while True:
        try:
            time.sleep(30)  # 每 30 秒执行一次
            now = time.time()
            if now - last_url_refresh >= url_refresh_interval:
                _refresh_proxy_urls_once()
                last_url_refresh = now
            database.reclaim_stale_assigned_tasks(timeout_seconds=120)
            database.distribute_detection_tasks()
            database.calculate_proxy_pool_tiers()
            database.cleanup_dead_proxies(days_inactive=1)
            database.cleanup_failed_proxies(fail_threshold=5)
            database.deduplicate_proxy_pool()
            if task_retention_days > 0 and now - last_task_cleanup >= task_cleanup_interval:
                deleted_tasks = database.cleanup_finished_tasks(retention_days=task_retention_days)
                if deleted_tasks > 0:
                    logger.info("已清理过期历史任务: %s 条", deleted_tasks)
                last_task_cleanup = now
            if now - last_pool_export >= pool_export_interval:
                _export_pool_once()
                last_pool_export = now
        except Exception:
            logger.exception("后台任务循环出现异常")


def _start_background_tasks() -> None:
    """启动后台定时任务线程。"""
    thread = threading.Thread(target=_background_task_loop, daemon=True)
    thread.start()
    logger.info("后台任务线程已启动")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/nodes/register", dependencies=[Depends(verify_node_token)])
def register_node(payload: RegisterNodeRequest) -> dict[str, str]:
    database.register_node(node_name=payload.node_name, worker_id=payload.worker_id)
    return {"status": "ok"}


@app.post("/nodes/allocate-name", response_model=AllocateNodeNameResponse, dependencies=[Depends(verify_node_token)])
def allocate_node_name(payload: AllocateNodeNameRequest) -> AllocateNodeNameResponse:
    node_name = database.allocate_node_name(country_code=payload.country_code)
    return AllocateNodeNameResponse(node_name=node_name)


@app.post("/nodes/heartbeat", dependencies=[Depends(verify_node_token)])
def heartbeat_node(payload: HeartbeatRequest) -> dict[str, str]:
    database.heartbeat_node(node_name=payload.node_name)
    return {"status": "ok"}


@app.post("/nodes/worker-heartbeat", dependencies=[Depends(verify_node_token)])
def heartbeat_worker(payload: WorkerHeartbeatRequest) -> dict[str, str]:
    database.heartbeat_worker(node_name=payload.node_name, worker_id=payload.worker_id)
    return {"status": "ok"}


@app.get("/nodes/online", response_model=list[NodeInfoView])
def get_online_nodes(heartbeat_timeout: int = Query(default=60, ge=10)) -> list[NodeInfoView]:
    nodes = database.list_active_nodes(heartbeat_timeout_sec=heartbeat_timeout)
    return [NodeInfoView(**node) for node in nodes]


@app.post("/proxies/import", response_model=ImportProxiesResponse)
def import_proxies(payload: ImportProxiesRequest) -> ImportProxiesResponse:
    imported, ignored = database.import_proxies(payload.proxies, payload.max_retries)
    return ImportProxiesResponse(imported=imported, ignored=ignored)


@app.post("/proxies/import/url", response_model=ImportProxiesResponse)
async def import_proxies_from_url(payload: ImportFromUrlRequest) -> ImportProxiesResponse:
    try:
        response = requests.get(payload.url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"无法读取网页内容: {exc}") from exc

    proxies = database.extract_proxies_from_text(response.text, source_hint=payload.url)
    imported, ignored = database.import_proxies(proxies, payload.max_retries)
    return ImportProxiesResponse(imported=imported, ignored=ignored)


@app.post("/proxies/import/urls", response_model=ImportProxiesResponse)
async def import_proxies_from_urls(payload: ImportFromUrlsRequest) -> ImportProxiesResponse:
    def fetch_url(url: str) -> list[str]:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return database.extract_proxies_from_text(response.text, source_hint=url)
        except requests.RequestException:
            return []

    results = await asyncio.gather(*[asyncio.to_thread(fetch_url, url) for url in payload.urls])

    all_proxies = []
    seen = set()
    for proxies_list in results:
        for proxy_url in proxies_list:
            if proxy_url not in seen:
                all_proxies.append(proxy_url)
                seen.add(proxy_url)

    imported, ignored = database.import_proxies(all_proxies, payload.max_retries)
    return ImportProxiesResponse(imported=imported, ignored=ignored)


@app.post("/proxies/import/file", response_model=ImportProxiesResponse)
async def import_proxies_from_file(
    file: UploadFile = File(...),
    max_retries: int = Query(default=2, ge=0, le=10),
) -> ImportProxiesResponse:
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")
    proxies = database.extract_proxies_from_text(text, source_hint=file.filename or "")
    imported, ignored = database.import_proxies(proxies, max_retries)
    return ImportProxiesResponse(imported=imported, ignored=ignored)


@app.get("/proxies", response_model=list[ProxyView])
def get_proxies(
    status: str | None = Query(default=None),
    pool_tier: str | None = Query(default=None),
    limit: int = Query(default=100, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ProxyView]:
    rows = database.list_proxies(status=status, pool_tier=pool_tier, limit=limit, offset=offset)
    return [ProxyView(**row) for row in rows]


@app.get("/tasks", response_model=list[TaskView])
def get_tasks(
    limit: int = Query(default=100, ge=0, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[TaskView]:
    rows = database.list_tasks(limit=limit, offset=offset)
    return [TaskView(**row) for row in rows]


@app.get("/stats", response_model=StatsResponse)
def get_stats() -> StatsResponse:
    return StatsResponse(**database.stats())


@app.get("/analytics/pool-tier-trend", response_model=list[PoolTierTrendPoint])
def get_pool_tier_trend(days: int = Query(default=14, ge=1, le=180)) -> list[PoolTierTrendPoint]:
    rows = database.pool_tier_trend(days=days)
    return [PoolTierTrendPoint(**row) for row in rows]


@app.get("/analytics/alive-protocol-distribution", response_model=AliveProtocolDistributionResponse)
def get_alive_protocol_distribution() -> AliveProtocolDistributionResponse:
    payload = database.alive_protocol_distribution()
    distribution = [ProtocolDistributionItem(**item) for item in payload.get("distribution", [])]
    return AliveProtocolDistributionResponse(total_alive=int(payload.get("total_alive", 0)), distribution=distribution)


@app.post("/pool/recalculate")
def recalculate_pool_tiers() -> dict[str, str]:
    database.calculate_proxy_pool_tiers()
    return {"status": "ok"}


@app.post("/pool/cleanup")
def cleanup_dead_proxies(days: int = Query(default=1, ge=1)) -> dict[str, int]:
    deleted = database.cleanup_dead_proxies(days_inactive=days)
    return {"deleted": deleted}


@app.post("/pool/cleanup-failed")
def cleanup_failed_proxies(fail_threshold: int = Query(default=5, ge=1)) -> dict[str, int]:
    deleted = database.cleanup_failed_proxies(fail_threshold=fail_threshold)
    return {"deleted": deleted}


@app.post("/distribution/dispatch")
def dispatch_detection_tasks() -> dict[str, str]:
    database.distribute_detection_tasks()
    return {"status": "ok"}


@app.post("/node/pull-task", response_model=PullTaskResponse, dependencies=[Depends(verify_node_token)])
def pull_task(payload: PullTaskRequest) -> PullTaskResponse:
    task = database.assign_next_task(node_name=payload.node_name, worker_id=payload.worker_id)
    if task is None:
        return PullTaskResponse(found=False, task=None)
    return PullTaskResponse(found=True, task=TaskPayload(**task))


@app.post("/node/push-result/{task_id}", dependencies=[Depends(verify_node_token)])
def push_result(task_id: str, payload: PushResultRequest) -> dict[str, str]:
    ok, message = database.submit_result(
        task_id=task_id,
        node_name=payload.node_name,
        worker_id=payload.worker_id,
        success=payload.success,
        latency_ms=payload.latency_ms,
        response_status=payload.response_status,
        error=payload.error,
        country_code=payload.country_code,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "ok"}
