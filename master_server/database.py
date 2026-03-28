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


def _monitor_interval_seconds() -> int:
    raw = os.getenv("MASTER_MONITOR_INTERVAL_SECONDS", "900")
    try:
        value = int(raw)
    except ValueError:
        return 900
    return max(60, value)


def _dispatch_batch_cap() -> int:
    raw = os.getenv("MASTER_DISPATCH_BATCH_CAP", "120")
    try:
        value = int(raw)
    except ValueError:
        return 120
    return max(5, value)


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

            CREATE TABLE IF NOT EXISTS proxy_blacklist (
                proxy_url TEXT PRIMARY KEY,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduler_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks(status, id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_finished_at ON tasks(status, finished_at);
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
        _ensure_column(conn, "proxies", "queue_order", "INTEGER")
        _ensure_column(conn, "proxies", "flow_phase", "TEXT NOT NULL DEFAULT 'primary_pending'")
        _ensure_column(conn, "check_results", "country_code", "TEXT")
        _ensure_column(conn, "tasks", "detect_stage", "TEXT NOT NULL DEFAULT 'primary'")
        _ensure_column(conn, "tasks", "batch_id", "TEXT")
        _normalize_task_uuids(conn)
        _ensure_scheduler_state(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxies_queue_order ON proxies(queue_order)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proxies_flow_phase ON proxies(flow_phase)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_stage_status ON tasks(detect_stage, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_batch_id ON tasks(batch_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_task_uuid ON tasks(task_uuid)")
        conn.commit()
    finally:
        conn.close()


def _ensure_scheduler_state(conn: sqlite3.Connection) -> None:
    defaults = {
        "scan_phase": "primary",
        "scan_round": "1",
    }
    for key, value in defaults.items():
        conn.execute(
            """
            INSERT INTO scheduler_state (state_key, state_value)
            VALUES (?, ?)
            ON CONFLICT(state_key) DO NOTHING
            """,
            (key, value),
        )


def _state_get(conn: sqlite3.Connection, key: str, default_value: str) -> str:
    row = conn.execute("SELECT state_value FROM scheduler_state WHERE state_key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO scheduler_state (state_key, state_value) VALUES (?, ?)",
            (key, default_value),
        )
        return default_value
    return str(row["state_value"])


def _state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO scheduler_state (state_key, state_value)
        VALUES (?, ?)
        ON CONFLICT(state_key) DO UPDATE SET state_value = excluded.state_value
        """,
        (key, value),
    )


def reset_detection_flow() -> None:
    """按新规则重置流程：从队列头开始完整首轮扫描。"""
    conn = _get_conn()
    try:
        with _DB_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM check_results")

            rows = conn.execute("SELECT id FROM proxies ORDER BY id ASC").fetchall()
            for idx, row in enumerate(rows, start=1):
                conn.execute(
                    """
                    UPDATE proxies
                    SET queue_order = ?,
                        flow_phase = 'primary_pending',
                        status = 'unknown',
                        pool_tier = 'unknown',
                        country_code = NULL,
                        latency_ms = NULL,
                        error = NULL,
                        last_checked_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (idx, _utcnow(), row["id"]),
                )

            _state_set(conn, "scan_phase", "primary")
            _state_set(conn, "scan_round", "1")
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
            max_order_row = conn.execute("SELECT COALESCE(MAX(queue_order), 0) AS max_order FROM proxies").fetchone()
            next_order = int(max_order_row["max_order"] or 0) + 1
            blacklisted_rows = conn.execute("SELECT proxy_url FROM proxy_blacklist").fetchall()
            blacklisted = {str(row["proxy_url"]).strip().lower() for row in blacklisted_rows}

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
                        if proxy_url.lower() in blacklisted:
                            ignored += 1
                            continue
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO proxies (proxy_url, protocol, status, pool_tier, queue_order, flow_phase, created_at, updated_at)
                            VALUES (?, ?, 'unknown', 'unknown', ?, 'primary_pending', ?, ?)
                            """,
                            (proxy_url, protocol, next_order, now, now),
                        )
                        if cursor.rowcount > 0:
                            imported += 1
                            next_order += 1
                        else:
                            ignored += 1
                    continue

                if not normalized:
                    ignored += 1
                    continue

                proxy_url, protocol = normalized
                if proxy_url.lower() in blacklisted:
                    ignored += 1
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO proxies (proxy_url, protocol, status, pool_tier, queue_order, flow_phase, created_at, updated_at)
                    VALUES (?, ?, 'unknown', 'unknown', ?, 'primary_pending', ?, ?)
                    """,
                    (proxy_url, protocol, next_order, now, now),
                )
                if cursor.rowcount > 0:
                    imported += 1
                    next_order += 1
                else:
                    ignored += 1

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
            # 先取精准绑定任务；取不到时允许同节点 worker 兜底领取，避免单 worker 堵塞导致 pending 长期堆积。
            row = conn.execute(
                """
                SELECT t.id, t.task_uuid, t.proxy_id, t.attempt, t.max_retries, p.proxy_url, p.protocol
                FROM tasks t
                JOIN proxies p ON p.id = t.proxy_id
                WHERE t.status = 'pending'
                  AND t.assigned_to = ?
                  AND t.assigned_worker_id = ?
                ORDER BY t.id ASC
                LIMIT 1
                """
                ,
                (node_name, worker_id),
            ).fetchone()

            if row is None:
                row = conn.execute(
                    """
                    SELECT t.id, t.task_uuid, t.proxy_id, t.attempt, t.max_retries, p.proxy_url, p.protocol
                    FROM tasks t
                    JOIN proxies p ON p.id = t.proxy_id
                    WHERE t.status = 'pending'
                      AND t.assigned_to = ?
                    ORDER BY t.id ASC
                    LIMIT 1
                    """,
                    (node_name,),
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
                SET status = 'pending', assigned_at = NULL, assigned_worker_id = NULL
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

            batch_id = str(task_row["batch_id"] or "")
            stage = str(task_row["detect_stage"] or "primary")

            total_in_batch = conn.execute(
                "SELECT COUNT(1) AS c FROM tasks WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["c"]
            finished_in_batch = conn.execute(
                "SELECT COUNT(1) AS c FROM tasks WHERE batch_id = ? AND status IN ('done', 'failed')",
                (batch_id,),
            ).fetchone()["c"]

            if int(total_in_batch or 0) > 0 and int(finished_in_batch or 0) >= int(total_in_batch or 0):
                success_count = conn.execute(
                    "SELECT COUNT(1) AS c FROM tasks WHERE batch_id = ? AND status = 'done'",
                    (batch_id,),
                ).fetchone()["c"]

                if int(success_count or 0) >= 3:
                    new_tier = "excellent"
                    new_status = "alive"
                elif int(success_count or 0) >= 1:
                    new_tier = "good"
                    new_status = "alive"
                else:
                    new_tier = "bad"
                    new_status = "dead"

                if stage == "primary":
                    new_phase = "primary_done"
                elif stage in {"secondary_bad", "secondary_good"}:
                    new_phase = "secondary_done"
                elif stage == "monitor":
                    new_phase = "monitor" if new_tier in {"excellent", "good"} else "monitor_excluded"
                else:
                    new_phase = "primary_done"

                conn.execute(
                    """
                    UPDATE proxies
                    SET status = ?,
                        pool_tier = ?,
                        flow_phase = ?,
                        country_code = COALESCE(?, country_code),
                        latency_ms = ?,
                        error = ?,
                        last_checked_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        new_tier,
                        new_phase,
                        normalized_country,
                        latency_ms,
                        error,
                        now,
                        now,
                        task_row["proxy_id"],
                    ),
                )

            conn.commit()
            return True, "ok"
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


def distribute_detection_tasks(max_concurrent_per_node: int = 5) -> None:
    """新版流程：首轮全量 -> 二轮 bad/good -> black list -> excellent/good 周期巡检。"""
    active_nodes = list_active_nodes()
    if len(active_nodes) < 3:
        return

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    monitor_cutoff = (now_dt - timedelta(seconds=_monitor_interval_seconds())).isoformat()
    batch_cap = _dispatch_batch_cap()

    conn = _get_conn()
    try:
        with _DB_LOCK:
            phase = _state_get(conn, "scan_phase", "primary")

            # URL 刷新后新代理会以 primary_pending 入队；若当前处于 monitor，需要切回 primary 才会触发首轮检测。
            if phase == "monitor":
                new_primary_pending = conn.execute(
                    "SELECT COUNT(1) AS c FROM proxies WHERE flow_phase = 'primary_pending'"
                ).fetchone()["c"]
                if int(new_primary_pending or 0) > 0:
                    _state_set(conn, "scan_phase", "primary")
                    phase = "primary"

            if phase == "primary":
                _dispatch_from_phase(conn, active_nodes, source_phase="primary_pending", stage="primary", batch_cap=batch_cap, now=now)
                primary_left = conn.execute(
                    "SELECT COUNT(1) AS c FROM proxies WHERE flow_phase IN ('primary_pending', 'primary_dispatched')"
                ).fetchone()["c"]
                primary_inflight = conn.execute(
                    "SELECT COUNT(1) AS c FROM tasks WHERE detect_stage = 'primary' AND status IN ('pending', 'assigned')"
                ).fetchone()["c"]
                if int(primary_left or 0) == 0 and int(primary_inflight or 0) == 0:
                    conn.execute(
                        "UPDATE proxies SET flow_phase = 'secondary_bad_pending' WHERE pool_tier = 'bad'"
                    )
                    conn.execute(
                        "UPDATE proxies SET flow_phase = 'secondary_good_pending' WHERE pool_tier = 'good'"
                    )
                    conn.execute(
                        "UPDATE proxies SET flow_phase = 'secondary_done' WHERE pool_tier = 'excellent'"
                    )
                    _state_set(conn, "scan_phase", "secondary_bad")

            elif phase == "secondary_bad":
                _dispatch_from_phase(conn, active_nodes, source_phase="secondary_bad_pending", stage="secondary_bad", batch_cap=batch_cap, now=now)
                left = conn.execute(
                    "SELECT COUNT(1) AS c FROM proxies WHERE flow_phase IN ('secondary_bad_pending', 'secondary_bad_dispatched')"
                ).fetchone()["c"]
                inflight = conn.execute(
                    "SELECT COUNT(1) AS c FROM tasks WHERE detect_stage = 'secondary_bad' AND status IN ('pending', 'assigned')"
                ).fetchone()["c"]
                if int(left or 0) == 0 and int(inflight or 0) == 0:
                    _state_set(conn, "scan_phase", "secondary_good")

            elif phase == "secondary_good":
                _dispatch_from_phase(conn, active_nodes, source_phase="secondary_good_pending", stage="secondary_good", batch_cap=batch_cap, now=now)
                left = conn.execute(
                    "SELECT COUNT(1) AS c FROM proxies WHERE flow_phase IN ('secondary_good_pending', 'secondary_good_dispatched')"
                ).fetchone()["c"]
                inflight = conn.execute(
                    "SELECT COUNT(1) AS c FROM tasks WHERE detect_stage = 'secondary_good' AND status IN ('pending', 'assigned')"
                ).fetchone()["c"]
                if int(left or 0) == 0 and int(inflight or 0) == 0:
                    _blacklist_and_prune_bad(conn, now=now)
                    conn.execute("UPDATE proxies SET flow_phase = 'monitor' WHERE pool_tier IN ('excellent', 'good')")
                    _state_set(conn, "scan_phase", "monitor")
                    current_round = int(_state_get(conn, "scan_round", "1") or "1")
                    _state_set(conn, "scan_round", str(current_round + 1))

            else:
                _dispatch_monitor(conn, active_nodes, batch_cap=batch_cap, monitor_cutoff=monitor_cutoff, now=now)

            conn.commit()
    finally:
        conn.close()


def _pick_three_workers(active_nodes: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    by_region: dict[str, list[dict[str, Any]]] = {}
    for node in active_nodes:
        region = _extract_region_from_node_name(str(node.get("node_name", ""))) or "ZZ"
        by_region.setdefault(region, []).append(node)

    if len(by_region) >= 3:
        regions = random.sample(list(by_region.keys()), 3)
        return [random.choice(by_region[region]) for region in regions]

    if len(active_nodes) < 3:
        return None
    return random.sample(active_nodes, 3)


def _proxy_has_inflight(conn: sqlite3.Connection, proxy_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM tasks
        WHERE proxy_id = ? AND status IN ('pending', 'assigned')
        LIMIT 1
        """,
        (proxy_id,),
    ).fetchone()
    return row is not None


def _dispatch_from_phase(
    conn: sqlite3.Connection,
    active_nodes: list[dict[str, Any]],
    source_phase: str,
    stage: str,
    batch_cap: int,
    now: str,
) -> None:
    for _ in range(batch_cap):
        proxy_row = conn.execute(
            """
            SELECT id
            FROM proxies
            WHERE flow_phase = ?
            ORDER BY queue_order ASC, id ASC
            LIMIT 1
            """,
            (source_phase,),
        ).fetchone()
        if proxy_row is None:
            break

        proxy_id = int(proxy_row["id"])
        if _proxy_has_inflight(conn, proxy_id):
            break

        workers = _pick_three_workers(active_nodes)
        if workers is None:
            break

        batch_id = _new_task_uuid()
        for node in workers:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_uuid, proxy_id, status, assigned_to, assigned_worker_id, assigned_at,
                    attempt, max_retries, created_at, detect_stage, batch_id
                )
                VALUES (?, ?, 'pending', ?, ?, NULL, 0, 0, ?, ?, ?)
                """,
                (
                    _new_task_uuid(),
                    proxy_id,
                    node["node_name"],
                    node["worker_id"],
                    now,
                    stage,
                    batch_id,
                ),
            )

        dispatched_phase = {
            "primary_pending": "primary_dispatched",
            "secondary_bad_pending": "secondary_bad_dispatched",
            "secondary_good_pending": "secondary_good_dispatched",
        }.get(source_phase, source_phase)
        conn.execute("UPDATE proxies SET flow_phase = ? WHERE id = ?", (dispatched_phase, proxy_id))


def _dispatch_monitor(
    conn: sqlite3.Connection,
    active_nodes: list[dict[str, Any]],
    batch_cap: int,
    monitor_cutoff: str,
    now: str,
) -> None:
    for _ in range(batch_cap):
        proxy_row = conn.execute(
            """
            SELECT id
            FROM proxies
            WHERE flow_phase = 'monitor'
              AND pool_tier IN ('excellent', 'good')
              AND (last_checked_at IS NULL OR last_checked_at < ?)
            ORDER BY queue_order ASC, id ASC
            LIMIT 1
            """,
            (monitor_cutoff,),
        ).fetchone()
        if proxy_row is None:
            break

        proxy_id = int(proxy_row["id"])
        if _proxy_has_inflight(conn, proxy_id):
            break

        workers = _pick_three_workers(active_nodes)
        if workers is None:
            break

        batch_id = _new_task_uuid()
        for node in workers:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_uuid, proxy_id, status, assigned_to, assigned_worker_id, assigned_at,
                    attempt, max_retries, created_at, detect_stage, batch_id
                )
                VALUES (?, ?, 'pending', ?, ?, NULL, 0, 0, ?, 'monitor', ?)
                """,
                (
                    _new_task_uuid(),
                    proxy_id,
                    node["node_name"],
                    node["worker_id"],
                    now,
                    batch_id,
                ),
            )


def _blacklist_and_prune_bad(conn: sqlite3.Connection, now: str) -> None:
    bad_rows = conn.execute("SELECT id, proxy_url FROM proxies WHERE pool_tier = 'bad'").fetchall()
    if not bad_rows:
        return

    for row in bad_rows:
        conn.execute(
            """
            INSERT INTO proxy_blacklist (proxy_url, reason, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(proxy_url) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at
            """,
            (row["proxy_url"], "second-pass-bad", now),
        )

    proxy_ids = [int(row["id"]) for row in bad_rows]
    _delete_proxies_by_ids(conn, proxy_ids)


def list_proxies(status: Optional[str], pool_tier: Optional[str], protocol: Optional[str], limit: int, offset: int) -> list[dict[str, Any]]:
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
        if protocol:
            where_clauses.append("lower(protocol) = lower(?)")
            params.append(protocol)

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
        SELECT proxy_id, node_name, worker_id, success, latency_ms, response_status, error, country_code, checked_at, id
        FROM check_results
        WHERE proxy_id IN ({placeholders})
        ORDER BY proxy_id ASC, checked_at DESC, id DESC
        """,
        proxy_ids,
    ).fetchall()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        proxy_id = row["proxy_id"]
        if len(grouped.get(proxy_id, [])) >= 5:
            continue
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


def pool_tier_trend(days: int = 14) -> list[dict[str, Any]]:
    day_count = max(1, min(180, int(days)))
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=day_count - 1)
    start_iso = start_date.isoformat()

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT substr(cr.checked_at, 1, 10) AS day,
                   CASE
                     WHEN lower(COALESCE(p.pool_tier, 'unknown')) IN ('excellent', 'good', 'bad')
                     THEN lower(p.pool_tier)
                     ELSE 'unknown'
                   END AS tier,
                   COUNT(DISTINCT cr.proxy_id) AS cnt
            FROM check_results cr
            JOIN proxies p ON p.id = cr.proxy_id
            WHERE substr(cr.checked_at, 1, 10) >= ?
            GROUP BY day, tier
            ORDER BY day ASC
            """,
            (start_iso,),
        ).fetchall()
    finally:
        conn.close()

    daily: dict[str, dict[str, int]] = {}
    for row in rows:
        day = str(row["day"])
        tier = str(row["tier"])
        cnt = int(row["cnt"] or 0)
        bucket = daily.setdefault(day, {"excellent": 0, "good": 0, "bad": 0, "unknown": 0})
        bucket[tier] = bucket.get(tier, 0) + cnt

    result: list[dict[str, Any]] = []
    for i in range(day_count):
        current_day = (start_date + timedelta(days=i)).isoformat()
        bucket = daily.get(current_day, {"excellent": 0, "good": 0, "bad": 0, "unknown": 0})
        total_checked = int(bucket["excellent"] + bucket["good"] + bucket["bad"] + bucket["unknown"])
        result.append(
            {
                "date": current_day,
                "excellent": int(bucket["excellent"]),
                "good": int(bucket["good"]),
                "bad": int(bucket["bad"]),
                "unknown": int(bucket["unknown"]),
                "total_checked": total_checked,
            }
        )

    return result


def alive_protocol_distribution() -> dict[str, Any]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT lower(COALESCE(protocol, 'unknown')) AS protocol, COUNT(1) AS cnt
            FROM proxies
            WHERE status = 'alive'
            GROUP BY lower(COALESCE(protocol, 'unknown'))
            ORDER BY cnt DESC, protocol ASC
            """
        ).fetchall()
    finally:
        conn.close()

    known_protocols = ["http", "https", "socks4", "socks5", "unknown"]
    counter: dict[str, int] = {name: 0 for name in known_protocols}

    for row in rows:
        protocol = str(row["protocol"] or "unknown")
        count = int(row["cnt"] or 0)
        if protocol not in counter:
            counter["unknown"] += count
        else:
            counter[protocol] += count

    distribution = [{"protocol": name, "count": int(counter[name])} for name in known_protocols]
    total_alive = sum(item["count"] for item in distribution)
    return {"total_alive": int(total_alive), "distribution": distribution}
