import os
import random
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

_DB_LOCK = threading.Lock()
_PROXY_PATTERN = re.compile(
    r"(?P<scheme>https?|socks4|socks5)://(?P<host>[A-Za-z0-9._-]+):(?P<port>\d{1,5})",
    flags=re.IGNORECASE,
)
_IP_PORT_PATTERN = re.compile(
    r"\b(?P<host>(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9._-]+):(?P<port>\d{1,5})\b",
    flags=re.IGNORECASE,
)
_COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_uuid() -> str:
    return str(uuid.uuid4())


def _extract_region_from_node_name(node_name: str) -> Optional[str]:
    text = (node_name or "").strip().upper()
    if not text:
        return None
    # Node names are expected to follow AA-node-BB, where AA is region/country code.
    if len(text) >= 2 and text[0:2].isalpha():
        return text[0:2]
    return None


def _infer_protocols_from_source(source_hint: Optional[str]) -> list[str]:
    all_protocols = ["http", "https", "socks4", "socks5"]
    if not source_hint:
        return all_protocols

    text = source_hint.lower()
    matched: list[str] = []

    if "socks4" in text:
        matched.append("socks4")
    if "socks5" in text:
        matched.append("socks5")

    if "http" in text and "socks" not in text:
        matched.append("http")
    if "https" in text and "socks" not in text:
        matched.append("https")

    if not matched:
        return all_protocols

    seen: set[str] = set()
    result: list[str] = []
    for p in matched:
        if p in seen:
            continue
        seen.add(p)
        result.append(p)
    return result


def _db_path() -> str:
    return os.getenv("MASTER_DB_PATH", "proxy_checker.db")


def _excellent_success_rate_threshold() -> float:
    raw = os.getenv("MASTER_EXCELLENT_SUCCESS_RATE", "0.8")
    try:
        value = float(raw)
    except ValueError:
        return 0.8
    return min(1.0, max(0.5, value))


def _excellent_min_checks() -> int:
    raw = os.getenv("MASTER_EXCELLENT_MIN_CHECKS", "3")
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(20, value))


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_url TEXT NOT NULL UNIQUE,
                protocol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                pool_tier TEXT NOT NULL DEFAULT 'unknown',
                country_code TEXT,
                latency_ms INTEGER,
                error TEXT,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_uuid TEXT NOT NULL UNIQUE,
                proxy_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                assigned_to TEXT,
                assigned_worker_id TEXT,
                assigned_at TEXT,
                finished_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2,
                created_at TEXT NOT NULL,
                FOREIGN KEY (proxy_id) REFERENCES proxies(id)
            );

            CREATE TABLE IF NOT EXISTS check_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                proxy_id INTEGER NOT NULL,
                node_name TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                success INTEGER NOT NULL,
                latency_ms INTEGER,
                response_status INTEGER,
                error TEXT,
                country_code TEXT,
                checked_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (proxy_id) REFERENCES proxies(id)
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL UNIQUE,
                worker_id TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS node_workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(node_name, worker_id)
            );

            CREATE TABLE IF NOT EXISTS node_name_registry (
                node_name TEXT PRIMARY KEY,
                country_code TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(country_code, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks(status, id);
            CREATE INDEX IF NOT EXISTS idx_tasks_proxy_id ON tasks(proxy_id);
            CREATE INDEX IF NOT EXISTS idx_results_proxy_node_time ON check_results(proxy_id, node_name, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_proxies_pool_tier ON proxies(pool_tier);
            CREATE INDEX IF NOT EXISTS idx_nodes_active ON nodes(active, last_heartbeat);
            CREATE INDEX IF NOT EXISTS idx_node_workers_active ON node_workers(active, last_heartbeat);
            CREATE INDEX IF NOT EXISTS idx_node_name_registry_country_seq ON node_name_registry(country_code, sequence);
            """
        )
        _ensure_column(conn, "tasks", "assigned_worker_id", "TEXT")
        _ensure_column(conn, "tasks", "task_uuid", "TEXT")
        _ensure_column(conn, "proxies", "pool_tier", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "proxies", "country_code", "TEXT")
        _ensure_column(conn, "check_results", "country_code", "TEXT")
        _normalize_task_uuids(conn)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_task_uuid ON tasks(task_uuid)")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_def: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(col[1] == column_name for col in columns):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


def _normalize_task_uuids(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, task_uuid FROM tasks ORDER BY id ASC").fetchall()
    seen: set[str] = set()
    for row in rows:
        existing = (row["task_uuid"] or "").strip() if row["task_uuid"] is not None else ""
        if not existing or existing in seen:
            conn.execute(
                "UPDATE tasks SET task_uuid = ? WHERE id = ?",
                (_new_task_uuid(), row["id"]),
            )
        else:
            seen.add(existing)


def register_node(node_name: str, worker_id: str) -> None:
    now = _utcnow()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute(
                """
                INSERT INTO node_workers (node_name, worker_id, last_heartbeat, registered_at, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(node_name, worker_id) DO UPDATE SET
                    last_heartbeat = excluded.last_heartbeat,
                    active = 1
                """,
                (node_name, worker_id, now, now),
            )
            conn.commit()
    finally:
        conn.close()


def allocate_node_name(country_code: str) -> str:
    country = (country_code or "ZZ").strip().upper()
    if not _COUNTRY_CODE_PATTERN.match(country):
        country = "ZZ"

    now = _utcnow()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM node_name_registry WHERE country_code = ?",
                (country,),
            ).fetchone()
            next_seq = int(row["max_sequence"]) + 1
            node_name = f"{country}-node-{next_seq:02d}"

            conn.execute(
                """
                INSERT INTO node_name_registry (node_name, country_code, sequence, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (node_name, country, next_seq, now),
            )
            conn.commit()
            return node_name
    finally:
        conn.close()


def heartbeat_node(node_name: str) -> None:
    now = _utcnow()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute(
                "UPDATE node_workers SET last_heartbeat = ? WHERE node_name = ?",
                (now, node_name),
            )
            conn.commit()
    finally:
        conn.close()


def heartbeat_worker(node_name: str, worker_id: str) -> None:
    now = _utcnow()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute(
                """
                UPDATE node_workers
                SET last_heartbeat = ?, active = 1
                WHERE node_name = ? AND worker_id = ?
                """,
                (now, node_name, worker_id),
            )
            conn.commit()
    finally:
        conn.close()


def list_active_nodes(heartbeat_timeout_sec: int = 60) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=heartbeat_timeout_sec)
    cutoff_str = cutoff.isoformat()

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT node_name, worker_id, last_heartbeat, registered_at
            FROM node_workers
            WHERE active = 1 AND last_heartbeat > ?
            ORDER BY node_name ASC, worker_id ASC
            """,
            (cutoff_str,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def extract_proxies_from_text(content: str, source_hint: Optional[str] = None) -> list[str]:
    proxies: list[str] = []
    seen: set[str] = set()

    for match in _PROXY_PATTERN.finditer(content):
        candidate = match.group(0).strip()
        normalized = _normalize_proxy(candidate)
        if not normalized:
            continue
        proxy_url, _ = normalized
        if proxy_url in seen:
            continue
        seen.add(proxy_url)
        proxies.append(proxy_url)

    inferred_protocols = _infer_protocols_from_source(source_hint)
    for match in _IP_PORT_PATTERN.finditer(content):
        start = match.start()
        if content[max(0, start - 3) : start] == "://":
            continue

        host = match.group("host")
        port = match.group("port")
        for protocol in inferred_protocols:
            candidate = f"{protocol}://{host}:{port}"
            normalized = _normalize_proxy(candidate)
            if not normalized:
                continue
            proxy_url, _ = normalized
            if proxy_url in seen:
                continue
            seen.add(proxy_url)
            proxies.append(proxy_url)

    return proxies


def _normalize_proxy(proxy_url: str) -> Optional[tuple[str, str]]:
    parsed = urlparse(proxy_url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks4", "socks5"}:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    if not parsed.hostname or not port:
        return None

    if port < 1 or port > 65535:
        return None

    host = parsed.hostname.lower()
    normalized = f"{scheme}://{host}:{port}"
    return normalized, scheme


def deduplicate_proxy_pool() -> int:
    """对整个代理池执行去重，返回被删除的重复代理数量。"""
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, proxy_url, status, updated_at
                FROM proxies
                ORDER BY id ASC
                """
            ).fetchall()

            groups: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                normalized = _normalize_proxy(row["proxy_url"])
                if normalized:
                    key = normalized[0]
                else:
                    key = row["proxy_url"].strip().lower()
                groups.setdefault(key, []).append(row)

            deleted_count = 0
            for canonical_url, proxy_rows in groups.items():
                if len(proxy_rows) == 1:
                    row = proxy_rows[0]
                    if row["proxy_url"] != canonical_url:
                        conn.execute("UPDATE proxies SET proxy_url = ? WHERE id = ?", (canonical_url, row["id"]))
                    continue

                keeper = max(
                    proxy_rows,
                    key=lambda r: (
                        1 if r["status"] == "alive" else 0,
                        r["updated_at"] or "",
                        -r["id"],
                    ),
                )
                keeper_id = keeper["id"]
                if keeper["proxy_url"] != canonical_url:
                    conn.execute("UPDATE proxies SET proxy_url = ? WHERE id = ?", (canonical_url, keeper_id))

                for row in proxy_rows:
                    proxy_id = row["id"]
                    if proxy_id == keeper_id:
                        continue
                    conn.execute("UPDATE tasks SET proxy_id = ? WHERE proxy_id = ?", (keeper_id, proxy_id))
                    conn.execute("UPDATE check_results SET proxy_id = ? WHERE proxy_id = ?", (keeper_id, proxy_id))
                    conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
                    deleted_count += 1

            conn.commit()
            return deleted_count
    finally:
        conn.close()


def import_proxies(proxy_urls: list[str], max_retries: int) -> tuple[int, int]:
    now = _utcnow()
    imported = 0
    ignored = 0

    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            for raw_url in proxy_urls:
                normalized = _normalize_proxy(raw_url)
                if not normalized:
                    expanded = extract_proxies_from_text(str(raw_url), source_hint=None)
                    if not expanded:
                        ignored += 1
                        continue
                    # For plain ip:port supplied directly, expand to multiple protocols.
                    for expanded_url in expanded:
                        expanded_normalized = _normalize_proxy(expanded_url)
                        if not expanded_normalized:
                            continue
                        proxy_url, protocol = expanded_normalized
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO proxies (proxy_url, protocol, status, created_at, updated_at)
                            VALUES (?, ?, 'unknown', ?, ?)
                            """,
                            (proxy_url, protocol, now, now),
                        )

                        row = conn.execute(
                            "SELECT id FROM proxies WHERE proxy_url = ?",
                            (proxy_url,),
                        ).fetchone()
                        if row is None:
                            ignored += 1
                            continue

                        proxy_id = row["id"]
                        active_task = conn.execute(
                            """
                            SELECT 1 FROM tasks
                            WHERE proxy_id = ? AND status IN ('pending', 'assigned')
                            LIMIT 1
                            """,
                            (proxy_id,),
                        ).fetchone()
                        if active_task:
                            ignored += 1
                            continue

                        conn.execute(
                            """
                            INSERT INTO tasks (task_uuid, proxy_id, status, attempt, max_retries, created_at)
                            VALUES (?, ?, 'pending', 0, ?, ?)
                            """,
                            (_new_task_uuid(), proxy_id, max_retries, now),
                        )
                        imported += 1
                    continue

                if not normalized:
                    ignored += 1
                    continue

                proxy_url, protocol = normalized
                conn.execute(
                    """
                    INSERT OR IGNORE INTO proxies (proxy_url, protocol, status, created_at, updated_at)
                    VALUES (?, ?, 'unknown', ?, ?)
                    """,
                    (proxy_url, protocol, now, now),
                )

                row = conn.execute(
                    "SELECT id FROM proxies WHERE proxy_url = ?",
                    (proxy_url,),
                ).fetchone()
                if row is None:
                    ignored += 1
                    continue

                proxy_id = row["id"]
                active_task = conn.execute(
                    """
                    SELECT 1 FROM tasks
                    WHERE proxy_id = ? AND status IN ('pending', 'assigned')
                    LIMIT 1
                    """,
                    (proxy_id,),
                ).fetchone()
                if active_task:
                    ignored += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO tasks (task_uuid, proxy_id, status, attempt, max_retries, created_at)
                    VALUES (?, ?, 'pending', 0, ?, ?)
                    """,
                    (_new_task_uuid(), proxy_id, max_retries, now),
                )
                imported += 1

            conn.commit()
        return imported, ignored
    finally:
        conn.close()


def assign_next_task(node_name: str, worker_id: str) -> Optional[dict[str, Any]]:
    now = _utcnow()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT t.id, t.task_uuid, t.proxy_id, t.attempt, t.max_retries, p.proxy_url, p.protocol
                FROM tasks t
                JOIN proxies p ON p.id = t.proxy_id
                                WHERE t.status = 'pending'
                                    AND (t.assigned_to IS NULL OR t.assigned_to = ?)
                                    AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id = ?)
                ORDER BY t.id ASC
                LIMIT 1
                """
                                ,
                                (node_name, worker_id),
            ).fetchone()

            if row is None:
                conn.commit()
                return None

            conn.execute(
                """
                UPDATE tasks
                SET status = 'assigned', assigned_to = ?, assigned_worker_id = ?, assigned_at = ?
                WHERE id = ?
                """,
                (node_name, worker_id, now, row["id"]),
            )
            conn.commit()

            return {
                "task_id": row["task_uuid"],
                "proxy_id": row["proxy_id"],
                "proxy_url": row["proxy_url"],
                "protocol": row["protocol"],
                "attempt": row["attempt"],
                "max_retries": row["max_retries"],
            }
    finally:
        conn.close()


def reclaim_stale_assigned_tasks(timeout_seconds: int = 120) -> int:
    """回收分配超时但未完成的任务，返回回收数量。"""
    if timeout_seconds < 1:
        timeout_seconds = 1

    now = _utcnow()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            reclaimed = conn.execute(
                """
                UPDATE tasks
                SET status = 'pending', assigned_at = NULL
                WHERE status = 'assigned' AND assigned_at IS NOT NULL AND assigned_at < ?
                """,
                (cutoff,),
            ).rowcount

            if reclaimed > 0:
                conn.execute(
                    """
                    UPDATE tasks
                    SET created_at = ?
                    WHERE status = 'pending' AND created_at < ?
                    """,
                    (now, cutoff),
                )
            conn.commit()
            return reclaimed
    finally:
        conn.close()


def submit_result(
    task_id: str,
    node_name: str,
    worker_id: str,
    success: bool,
    latency_ms: Optional[int],
    response_status: Optional[int],
    error: Optional[str],
    country_code: Optional[str],
) -> tuple[bool, str]:
    now = _utcnow()
    normalized_country: Optional[str] = None
    if country_code:
        maybe = country_code.strip().upper()
        if len(maybe) == 2 and maybe.isalpha():
            normalized_country = maybe

    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE task_uuid = ?",
                (task_id,),
            ).fetchone()
            if task_row is None and str(task_id).isdigit():
                task_row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (int(task_id),),
                ).fetchone()
            if task_row is None:
                conn.commit()
                return False, "Task not found"

            assigned_to = task_row["assigned_to"]
            if assigned_to and assigned_to != node_name:
                conn.commit()
                return False, "Task is assigned to another node"

            task_status = "done" if success else "failed"
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?
                WHERE id = ?
                """,
                (task_status, now, task_row["id"]),
            )

            proxy_status = "alive" if success else "dead"
            conn.execute(
                """
                UPDATE proxies
                SET status = ?, country_code = COALESCE(?, country_code), latency_ms = ?, error = ?, last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (proxy_status, normalized_country, latency_ms, error, now, now, task_row["proxy_id"]),
            )

            conn.execute(
                """
                INSERT INTO check_results (
                    task_id, proxy_id, node_name, worker_id, success, latency_ms, response_status, error, country_code, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_row["id"],
                    task_row["proxy_id"],
                    node_name,
                    worker_id,
                    1 if success else 0,
                    latency_ms,
                    response_status,
                    error,
                    normalized_country,
                    now,
                ),
            )

            if not success and task_row["attempt"] < task_row["max_retries"]:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_uuid, proxy_id, status, assigned_to, assigned_worker_id, attempt, max_retries, created_at
                    )
                    VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_task_uuid(),
                        task_row["proxy_id"],
                        node_name,
                        worker_id,
                        task_row["attempt"] + 1,
                        task_row["max_retries"],
                        now,
                    ),
                )

            conn.commit()
            return True, "ok"
    finally:
        conn.close()


def calculate_proxy_pool_tiers() -> None:
    """根据检测结果自动计算代理池等级：excellent（高成功率）、good（部分成功）、bad（全部失败）。"""
    excellent_threshold = _excellent_success_rate_threshold()
    excellent_min_checks = _excellent_min_checks()
    conn = _get_conn()
    try:
        with _DB_LOCK:
            proxies = conn.execute("SELECT id, proxy_url FROM proxies").fetchall()
            for proxy_row in proxies:
                proxy_id = proxy_row["id"]
                results = conn.execute(
                    """
                    SELECT COALESCE(COUNT(*), 0) as total, SUM(success) as success_count
                    FROM check_results
                    WHERE proxy_id = ? AND checked_at > datetime('now', '-7 days')
                    """,
                    (proxy_id,),
                ).fetchone()

                if not results or results["total"] == 0:
                    pool_tier = "unknown"
                else:
                    total = results["total"]
                    success = results["success_count"] or 0
                    success_rate = success / total
                    if total >= excellent_min_checks and success_rate >= excellent_threshold:
                        pool_tier = "excellent"
                    elif success > 0:
                        pool_tier = "good"
                    else:
                        pool_tier = "bad"

                conn.execute(
                    "UPDATE proxies SET pool_tier = ? WHERE id = ?",
                    (pool_tier, proxy_id),
                )
            conn.commit()
    finally:
        conn.close()


def _delete_proxies_by_ids(conn: sqlite3.Connection, proxy_ids: list[int]) -> int:
    if not proxy_ids:
        return 0

    placeholders = ",".join(["?"] * len(proxy_ids))
    conn.execute(f"DELETE FROM tasks WHERE proxy_id IN ({placeholders})", proxy_ids)
    conn.execute(f"DELETE FROM check_results WHERE proxy_id IN ({placeholders})", proxy_ids)
    deleted = conn.execute(f"DELETE FROM proxies WHERE id IN ({placeholders})", proxy_ids).rowcount
    return deleted


def cleanup_dead_proxies(days_inactive: int = 1) -> int:
    """清理长期不可用（status=dead 且 pool_tier=bad）的代理，返回删除数量。"""
    conn = _get_conn()
    try:
        with _DB_LOCK:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_inactive)
            cutoff_str = cutoff.isoformat()

            rows = conn.execute(
                """
                SELECT id FROM proxies
                WHERE status = 'dead' AND pool_tier = 'bad' AND updated_at < ?
                """,
                (cutoff_str,),
            ).fetchall()
            proxy_ids = [row["id"] for row in rows]
            deleted = _delete_proxies_by_ids(conn, proxy_ids)
            conn.commit()
            return deleted
    finally:
        conn.close()


def cleanup_failed_proxies(fail_threshold: int = 5) -> int:
    """清理累计失败次数达到阈值且从未成功过的代理。"""
    conn = _get_conn()
    try:
        with _DB_LOCK:
            rows = conn.execute(
                """
                SELECT p.id
                FROM proxies p
                JOIN (
                    SELECT proxy_id,
                           SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count
                    FROM check_results
                    GROUP BY proxy_id
                ) agg ON agg.proxy_id = p.id
                WHERE agg.fail_count >= ? AND COALESCE(agg.success_count, 0) = 0
                """,
                (fail_threshold,),
            ).fetchall()
            proxy_ids = [row["id"] for row in rows]
            deleted = _delete_proxies_by_ids(conn, proxy_ids)
            conn.commit()
            return deleted
    finally:
        conn.close()


def distribute_detection_tasks(max_concurrent_per_node: int = 5) -> None:
    """每 6 小时为每个代理随机分配到 3 个不同地区的在线 worker。"""
    active_nodes = list_active_nodes()
    if not active_nodes:
        return

    nodes_by_region: dict[str, list[dict[str, Any]]] = {}
    for node in active_nodes:
        region = _extract_region_from_node_name(str(node.get("node_name", "")))
        if not region:
            continue
        nodes_by_region.setdefault(region, []).append(node)

    if len(nodes_by_region) < 3:
        # 地区不足 3 个时按需求跳过本轮分发。
        return

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    six_hours_ago = (now_dt - timedelta(hours=6)).isoformat()
    candidate_regions = list(nodes_by_region.keys())

    conn = _get_conn()
    try:
        with _DB_LOCK:
            all_proxies = conn.execute(
                "SELECT id, last_checked_at FROM proxies ORDER BY RANDOM()"
            ).fetchall()

            for proxy_row in all_proxies:
                proxy_id = proxy_row["id"]

                # 当前代理有未完成任务时跳过，避免重复堆积。
                in_flight = conn.execute(
                    """
                    SELECT 1 FROM tasks
                    WHERE proxy_id = ? AND status IN ('pending', 'assigned')
                    LIMIT 1
                    """,
                    (proxy_id,),
                ).fetchone()
                if in_flight:
                    continue

                last_checked_at = proxy_row["last_checked_at"]
                if last_checked_at and last_checked_at >= six_hours_ago:
                    continue

                selected_regions = random.sample(candidate_regions, 3)
                for region in selected_regions:
                    selected_node = random.choice(nodes_by_region[region])
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_uuid, proxy_id, status, assigned_to, assigned_worker_id, attempt, max_retries, created_at
                        )
                        VALUES (?, ?, 'pending', ?, ?, 0, 2, ?)
                        """,
                        (
                            _new_task_uuid(),
                            proxy_id,
                            selected_node["node_name"],
                            selected_node["worker_id"],
                            now,
                        ),
                    )

            conn.commit()
    finally:
        conn.close()


def list_proxies(status: Optional[str], pool_tier: Optional[str], limit: int, offset: int) -> list[dict[str, Any]]:
    conn = _get_conn()
    try:
        where_clauses = []
        params: list[Any] = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if pool_tier:
            where_clauses.append("pool_tier = ?")
            params.append(pool_tier)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        if limit == 0:
            rows = conn.execute(
                f"""
                SELECT id, proxy_url, protocol, status, pool_tier, country_code, latency_ms, error, last_checked_at, updated_at
                FROM proxies
                {where_sql}
                ORDER BY id DESC
                """,
                tuple(params),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT id, proxy_url, protocol, status, pool_tier, country_code, latency_ms, error, last_checked_at, updated_at
                FROM proxies
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()

        proxy_rows = [dict(row) for row in rows]
        proxy_ids = [row["id"] for row in proxy_rows]
        node_results_map = _list_latest_node_results(conn, proxy_ids)

        for row in proxy_rows:
            row["node_results"] = node_results_map.get(row["id"], [])

        return proxy_rows
    finally:
        conn.close()


def _list_latest_node_results(conn: sqlite3.Connection, proxy_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not proxy_ids:
        return {}

    placeholders = ",".join(["?"] * len(proxy_ids))
    rows = conn.execute(
        f"""
        SELECT r1.proxy_id, r1.node_name, r1.worker_id, r1.success, r1.latency_ms, r1.response_status, r1.error, r1.country_code, r1.checked_at
        FROM check_results r1
        JOIN (
            SELECT proxy_id, node_name, worker_id, MAX(id) AS max_id
            FROM check_results
            WHERE proxy_id IN ({placeholders})
            GROUP BY proxy_id, node_name, worker_id
        ) latest ON latest.max_id = r1.id
        ORDER BY r1.proxy_id ASC, r1.node_name ASC, r1.worker_id ASC
        """,
        proxy_ids,
    ).fetchall()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        proxy_id = row["proxy_id"]
        grouped.setdefault(proxy_id, []).append(
            {
                "node_name": row["node_name"],
                "worker_id": row["worker_id"],
                "success": bool(row["success"]),
                "latency_ms": row["latency_ms"],
                "response_status": row["response_status"],
                "error": row["error"],
                "country_code": row["country_code"],
                "checked_at": row["checked_at"],
            }
        )
    return grouped


def export_alive_proxies_by_tier(export_dir: str) -> dict[str, int]:
    """将 status=alive 的代理导出到 output/<protocol>/ 下的分层文件。"""
    output_dir = (export_dir or "").strip()
    if not output_dir:
        return {}

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT proxy_url, protocol, pool_tier
            FROM proxies
            WHERE status = 'alive'
            ORDER BY protocol ASC, pool_tier ASC, proxy_url ASC
            """
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, dict[str, list[str]]] = {
        "http": {"excellent": [], "good": [], "bad": [], "unknown": []},
        "https": {"excellent": [], "good": [], "bad": [], "unknown": []},
        "socks4": {"excellent": [], "good": [], "bad": [], "unknown": []},
        "socks5": {"excellent": [], "good": [], "bad": [], "unknown": []},
    }

    for row in rows:
        protocol_raw = str(row["protocol"] or "").strip().lower()
        if protocol_raw not in groups:
            continue

        tier_raw = str(row["pool_tier"] or "unknown").strip().lower()
        if tier_raw == "excellent":
            bucket = "excellent"
        elif tier_raw == "good":
            bucket = "good"
        elif tier_raw == "bad":
            bucket = "bad"
        else:
            bucket = "unknown"
        groups[protocol_raw][bucket].append(str(row["proxy_url"]))

    output_root = os.path.join(output_dir, "output")
    os.makedirs(output_root, exist_ok=True)

    counts: dict[str, int] = {}
    for protocol, bucket_map in groups.items():
        protocol_dir = os.path.join(output_root, protocol)
        os.makedirs(protocol_dir, exist_ok=True)

        excellent_list = sorted(set(bucket_map["excellent"]))
        good_list = sorted(set(bucket_map["good"]))
        bad_list = sorted(set(bucket_map["bad"]))
        unknown_list = sorted(set(bucket_map["unknown"]))
        excellent_good_list = sorted(set(excellent_list + good_list))

        file_map = {
            "excellent.txt": excellent_list,
            "good.txt": good_list,
            "bad.txt": bad_list,
            "excellent+good.txt": excellent_good_list,
            "unknown.txt": unknown_list,
        }

        for file_name, proxies in file_map.items():
            file_path = os.path.join(protocol_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as fp:
                if proxies:
                    fp.write("\n".join(proxies) + "\n")
                else:
                    fp.write("")
            counts[f"{protocol}/{file_name}"] = len(proxies)

    return counts


def list_tasks(limit: int, offset: int) -> list[dict[str, Any]]:
    conn = _get_conn()
    try:
        if limit == 0:
            rows = conn.execute(
                """
                SELECT id, task_uuid, proxy_id, status, assigned_to, assigned_worker_id, attempt, max_retries, created_at, finished_at
                FROM tasks
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, task_uuid, proxy_id, status, assigned_to, assigned_worker_id, attempt, max_retries, created_at, finished_at
                FROM tasks
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def stats() -> dict[str, int]:
    conn = _get_conn()
    try:
        proxy_total = conn.execute("SELECT COUNT(1) FROM proxies").fetchone()[0]
        proxy_alive = conn.execute("SELECT COUNT(1) FROM proxies WHERE status = 'alive'").fetchone()[0]
        proxy_dead = conn.execute("SELECT COUNT(1) FROM proxies WHERE status = 'dead'").fetchone()[0]
        task_pending = conn.execute("SELECT COUNT(1) FROM tasks WHERE status = 'pending'").fetchone()[0]
        task_assigned = conn.execute("SELECT COUNT(1) FROM tasks WHERE status = 'assigned'").fetchone()[0]
        task_done = conn.execute("SELECT COUNT(1) FROM tasks WHERE status = 'done'").fetchone()[0]
        task_failed = conn.execute("SELECT COUNT(1) FROM tasks WHERE status = 'failed'").fetchone()[0]

        return {
            "proxy_total": proxy_total,
            "proxy_alive": proxy_alive,
            "proxy_dead": proxy_dead,
            "task_pending": task_pending,
            "task_assigned": task_assigned,
            "task_done": task_done,
            "task_failed": task_failed,
        }
    finally:
        conn.close()
