import argparse
import logging
import os
import threading
import time

import requests

from worker_node.gateway import MasterNodeGateway
from worker_node.logging_utils import setup_worker_logging
from worker_node.node import DetectorNode


def _resolve_node_token(node_token: str) -> str:
    explicit = node_token.strip()
    if explicit:
        return explicit

    env_token = os.getenv("NODE_TOKEN", "").strip()
    if env_token:
        return env_token

    token_file = os.getenv("NODE_TOKEN_FILE", "").strip()
    if token_file:
        try:
            wait_seconds = int(os.getenv("NODE_TOKEN_FILE_WAIT_SECONDS", "30"))
        except ValueError:
            wait_seconds = 30
        wait_seconds = max(0, wait_seconds)
        deadline = time.time() + wait_seconds
        while True:
            try:
                with open(token_file, "r", encoding="utf-8") as fp:
                    from_file = fp.read().strip()
                    if from_file:
                        return from_file
            except OSError:
                pass

            if time.time() >= deadline:
                break
            time.sleep(1)

    raise ValueError("未提供 --node-token，且无法从 NODE_TOKEN 或 NODE_TOKEN_FILE 获取令牌")


def _detect_country_code() -> str:
    services = [
        "https://ipapi.co/country/",
        "https://ifconfig.co/country-iso",
        "https://ipinfo.io/country",
    ]
    for url in services:
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            code = response.text.strip().upper()
            if len(code) == 2 and code.isalpha():
                return code
        except requests.RequestException:
            continue
    return "ZZ"


def main() -> None:
    parser = argparse.ArgumentParser(description="代理检测节点")
    parser.add_argument("--master-url", default="http://127.0.0.1:62071")
    parser.add_argument("--node-name", required=False, default=None, help="节点名称；不传则自动生成 AA-node-BB")
    parser.add_argument("--worker-id", default="worker")
    parser.add_argument("--worker-count", type=int, default=4, help="单节点并发 worker 数，默认 4")
    parser.add_argument("--node-token", default="", help="节点鉴权令牌；不传则尝试自动读取")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--target-url", default="https://httpbin.org/ip")
    parser.add_argument("--log-dir", default="", help="日志目录；默认 ./logs/worker")
    parser.add_argument("--log-level", default="INFO", help="日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--log-retention-days", type=int, default=14, help="日志保留天数，默认 14")

    args = parser.parse_args()
    resolved_token = _resolve_node_token(args.node_token)
    resolved_log_dir = args.log_dir.strip() or os.path.join(".", "logs", "worker")
    setup_worker_logging(
        log_dir=resolved_log_dir,
        level=args.log_level,
        retention_days=max(1, args.log_retention_days),
    )
    logger = logging.getLogger(__name__)
    logger.info("worker 启动参数: master_url=%s worker_count=%s", args.master_url, args.worker_count)

    node_name = args.node_name
    if not node_name:
        country_code = _detect_country_code()
        bootstrap_gateway = MasterNodeGateway(
            master_url=args.master_url,
            node_name="pending-node",
            worker_id=args.worker_id,
            node_token=resolved_token,
        )
        node_name = bootstrap_gateway.allocate_node_name(country_code=country_code)

    workers = max(1, args.worker_count)
    threads: list[threading.Thread] = []

    for i in range(workers):
        worker_id = args.worker_id if workers == 1 else f"{args.worker_id}-{i + 1}"
        gateway = MasterNodeGateway(
            master_url=args.master_url,
            node_name=node_name,
            worker_id=worker_id,
            node_token=resolved_token,
        )
        node = DetectorNode(
            gateway=gateway,
            poll_interval=args.interval,
            timeout=args.timeout,
            target_url=args.target_url,
            heartbeat_interval=args.heartbeat_interval,
        )
        thread = threading.Thread(target=node.run_forever, daemon=False)
        thread.start()
        threads.append(thread)
        logger.info("worker 线程已启动: %s", worker_id)

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
