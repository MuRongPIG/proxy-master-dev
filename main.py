import argparse
import logging
import os
import secrets
import threading
import time

import requests

from master_server.logging_utils import setup_master_logging
from worker_node.logging_utils import setup_worker_logging


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


def _resolve_master_node_token() -> str:
	env_token = os.getenv("NODE_TOKEN", "").strip()
	if env_token:
		return env_token

	raise ValueError("无法从 NODE_TOKEN 环境变量获取令牌，必须在 composer 中指定！")


def _resolve_worker_node_token() -> str:
	env_token = os.getenv("NODE_TOKEN", "").strip()
	if env_token:
		return env_token

	raise ValueError("无法从 NODE_TOKEN 环境变量获取令牌，必须在 composer 中指定！")


def _run_master(
	host: str,
	port: int,
	db_path: str,
	bootstrap_proxy_files: str,
	bootstrap_proxy_urls: str,
	bootstrap_proxy_url_file: str,
	url_refresh_interval_seconds: int,
	bootstrap_max_retries: int,
	pool_export_dir: str,
	pool_export_interval_seconds: int,
	excellent_success_rate: float,
	excellent_min_checks: int,
	log_dir: str,
	log_level: str,
	log_retention_days: int,
	ui_cors_origins: str,
) -> None:
	import uvicorn
	resolved_token = _resolve_master_node_token()
	resolved_log_dir = log_dir.strip() or os.path.join(os.path.dirname(db_path) or ".", "logs", "master")
	setup_master_logging(
		log_dir=resolved_log_dir,
		level=log_level,
		retention_days=max(1, log_retention_days),
	)

	os.environ["MASTER_DB_PATH"] = db_path
	os.environ["NODE_API_TOKEN"] = resolved_token
	os.environ["MASTER_BOOTSTRAP_PROXY_FILES"] = bootstrap_proxy_files
	os.environ["MASTER_BOOTSTRAP_PROXY_URLS"] = bootstrap_proxy_urls
	os.environ["MASTER_BOOTSTRAP_PROXY_URL_FILE"] = bootstrap_proxy_url_file
	os.environ["MASTER_URL_REFRESH_INTERVAL_SECONDS"] = str(url_refresh_interval_seconds)
	os.environ["MASTER_BOOTSTRAP_MAX_RETRIES"] = str(bootstrap_max_retries)
	os.environ["MASTER_POOL_EXPORT_DIR"] = pool_export_dir
	os.environ["MASTER_POOL_EXPORT_INTERVAL_SECONDS"] = str(pool_export_interval_seconds)
	os.environ["MASTER_EXCELLENT_SUCCESS_RATE"] = str(excellent_success_rate)
	os.environ["MASTER_EXCELLENT_MIN_CHECKS"] = str(excellent_min_checks)
	os.environ["MASTER_LOG_DIR"] = resolved_log_dir
	os.environ["MASTER_LOG_LEVEL"] = log_level
	os.environ["MASTER_LOG_RETENTION_DAYS"] = str(max(1, log_retention_days))
	os.environ["MASTER_UI_CORS_ORIGINS"] = ui_cors_origins
	uvicorn.run("master_server.api:app", host=host, port=port, reload=False)


def _run_worker(
	master_url: str,
	node_name: str | None,
	worker_id: str,
	worker_count: int,
	interval: float,
	timeout: float,
	heartbeat_interval: float,
	target_url: str,
	log_dir: str,
	log_level: str,
	log_retention_days: int,
) -> None:
	from worker_node.gateway import MasterNodeGateway
	from worker_node.node import DetectorNode
	resolved_token = _resolve_worker_node_token()
	resolved_log_dir = log_dir.strip() or os.path.join(".", "logs", "worker")
	setup_worker_logging(
		log_dir=resolved_log_dir,
		level=log_level,
		retention_days=max(1, log_retention_days),
	)
	logger = logging.getLogger(__name__)

	resolved_node_name = node_name
	if not resolved_node_name:
		country_code = _detect_country_code()
		bootstrap_gateway = MasterNodeGateway(
			master_url=master_url,
			node_name="pending-node",
			worker_id=worker_id,
			node_token=resolved_token,
		)
		resolved_node_name = bootstrap_gateway.allocate_node_name(country_code=country_code)

	workers = max(1, worker_count)
	threads: list[threading.Thread] = []

	for i in range(workers):
		current_worker_id = worker_id if workers == 1 else f"{worker_id}-{i + 1}"
		gateway = MasterNodeGateway(
			master_url=master_url,
			node_name=resolved_node_name,
			worker_id=current_worker_id,
			node_token=resolved_token,
		)
		node = DetectorNode(
			gateway=gateway,
			poll_interval=interval,
			timeout=timeout,
			target_url=target_url,
			heartbeat_interval=heartbeat_interval,
		)
		thread = threading.Thread(target=node.run_forever, daemon=False)
		thread.start()
		threads.append(thread)
		logger.info("worker 线程已启动: %s", current_worker_id)

	for thread in threads:
		thread.join()


def main() -> None:
	parser = argparse.ArgumentParser(description="分布式代理检测入口")
	subparsers = parser.add_subparsers(dest="role", required=True)

	master_parser = subparsers.add_parser("master", help="启动主服务器")
	master_parser.add_argument("--host", default="127.0.0.1")
	master_parser.add_argument("--port", type=int, default=62071)
	master_parser.add_argument("--db-path", default="proxy_checker.db")
	master_parser.add_argument("--bootstrap-proxy-files", default="")
	master_parser.add_argument("--bootstrap-proxy-urls", default="")
	master_parser.add_argument("--bootstrap-proxy-url-file", default="master_server/bootstrap_proxy_urls.txt")
	master_parser.add_argument("--url-refresh-interval-seconds", type=int, default=300)
	master_parser.add_argument("--bootstrap-max-retries", type=int, default=2)
	master_parser.add_argument("--pool-export-dir", default="")
	master_parser.add_argument("--pool-export-interval-seconds", type=int, default=300)
	master_parser.add_argument("--excellent-success-rate", type=float, default=0.8)
	master_parser.add_argument("--excellent-min-checks", type=int, default=3)
	master_parser.add_argument("--log-dir", default="")
	master_parser.add_argument("--log-level", default="INFO")
	master_parser.add_argument("--log-retention-days", type=int, default=14)
	master_parser.add_argument("--ui-cors-origins", default="*")

	worker_parser = subparsers.add_parser("worker", help="启动检测节点")
	worker_parser.add_argument("--master-url", default="http://127.0.0.1:62071")
	worker_parser.add_argument("--node-name", required=False, default=None, help="节点名称；不传则自动生成 AA-node-BB")
	worker_parser.add_argument("--worker-id", default="worker")
	worker_parser.add_argument("--worker-count", type=int, default=4)
	worker_parser.add_argument("--interval", type=float, default=2.0)
	worker_parser.add_argument("--timeout", type=float, default=8.0)
	worker_parser.add_argument("--heartbeat-interval", type=float, default=15.0)
	worker_parser.add_argument("--target-url", default="https://httpbin.org/ip")
	worker_parser.add_argument("--log-dir", default="")
	worker_parser.add_argument("--log-level", default="INFO")
	worker_parser.add_argument("--log-retention-days", type=int, default=14)

	args = parser.parse_args()

	if args.role == "master":
		_run_master(
			host=args.host,
			port=args.port,
			db_path=args.db_path,
			bootstrap_proxy_files=args.bootstrap_proxy_files,
			bootstrap_proxy_urls=args.bootstrap_proxy_urls,
			bootstrap_proxy_url_file=args.bootstrap_proxy_url_file,
			url_refresh_interval_seconds=args.url_refresh_interval_seconds,
			bootstrap_max_retries=args.bootstrap_max_retries,
			pool_export_dir=args.pool_export_dir,
			pool_export_interval_seconds=args.pool_export_interval_seconds,
			excellent_success_rate=args.excellent_success_rate,
			excellent_min_checks=args.excellent_min_checks,
			log_dir=args.log_dir,
			log_level=args.log_level,
			log_retention_days=args.log_retention_days,
			ui_cors_origins=args.ui_cors_origins,
		)
	elif args.role == "worker":
		_run_worker(
			master_url=args.master_url,
			node_name=args.node_name,
			worker_id=args.worker_id,
			worker_count=args.worker_count,
			interval=args.interval,
			timeout=args.timeout,
			heartbeat_interval=args.heartbeat_interval,
			target_url=args.target_url,
			log_dir=args.log_dir,
			log_level=args.log_level,
			log_retention_days=args.log_retention_days,
		)


if __name__ == "__main__":
	main()