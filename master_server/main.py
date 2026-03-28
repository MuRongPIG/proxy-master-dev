import argparse
import os

import uvicorn

from master_server.logging_utils import setup_master_logging


def _resolve_node_token() -> str:
    env_token = os.getenv("NODE_TOKEN", "").strip()
    if env_token:
        return env_token

    raise ValueError("无法从 NODE_TOKEN 环境变量获取令牌，必须在 composer 中指定！")


def main() -> None:
    parser = argparse.ArgumentParser(description="代理检测主服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=62071)
    parser.add_argument("--db-path", default="proxy_checker.db")
    parser.add_argument("--bootstrap-proxy-files", default="", help="启动时导入的本地代理文件，多个用逗号分隔")
    parser.add_argument("--bootstrap-proxy-urls", default="", help="启动时导入并定时刷新的代理 URL，多个用逗号分隔")
    parser.add_argument(
        "--bootstrap-proxy-url-file",
        default="master_server/bootstrap_proxy_urls.txt",
        help="包含代理 URL 列表的本地文件（每行一个 URL）",
    )
    parser.add_argument("--url-refresh-interval-seconds", type=int, default=300, help="定时从 URL 刷新代理的间隔秒数")
    parser.add_argument("--bootstrap-max-retries", type=int, default=2, help="自动导入任务的最大重试次数")
    parser.add_argument("--pool-export-dir", default="", help="代理池导出目录；默认 db-path 同目录下的 pool_exports")
    parser.add_argument("--pool-export-interval-seconds", type=int, default=300, help="代理池导出间隔秒数，最小 30")
    parser.add_argument("--log-dir", default="", help="日志目录；默认 db-path 同目录下 logs/master")
    parser.add_argument("--log-level", default="INFO", help="日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--log-retention-days", type=int, default=14, help="日志保留天数，默认 14")
    parser.add_argument("--ui-cors-origins", default="*", help="允许 UI 访问的 CORS 源，多个用逗号分隔")
    args = parser.parse_args()
    resolved_token = _resolve_node_token()

    resolved_log_dir = args.log_dir.strip() or os.path.join(os.path.dirname(args.db_path) or ".", "logs", "master")
    setup_master_logging(
        log_dir=resolved_log_dir,
        level=args.log_level,
        retention_days=max(1, args.log_retention_days),
    )

    os.environ["MASTER_DB_PATH"] = args.db_path
    os.environ["NODE_API_TOKEN"] = resolved_token
    os.environ["MASTER_BOOTSTRAP_PROXY_FILES"] = args.bootstrap_proxy_files
    os.environ["MASTER_BOOTSTRAP_PROXY_URLS"] = args.bootstrap_proxy_urls
    os.environ["MASTER_BOOTSTRAP_PROXY_URL_FILE"] = args.bootstrap_proxy_url_file
    os.environ["MASTER_URL_REFRESH_INTERVAL_SECONDS"] = str(args.url_refresh_interval_seconds)
    os.environ["MASTER_BOOTSTRAP_MAX_RETRIES"] = str(args.bootstrap_max_retries)
    os.environ["MASTER_POOL_EXPORT_DIR"] = args.pool_export_dir
    os.environ["MASTER_POOL_EXPORT_INTERVAL_SECONDS"] = str(args.pool_export_interval_seconds)
    os.environ["MASTER_LOG_DIR"] = resolved_log_dir
    os.environ["MASTER_LOG_LEVEL"] = args.log_level
    os.environ["MASTER_LOG_RETENTION_DAYS"] = str(max(1, args.log_retention_days))
    os.environ["MASTER_UI_CORS_ORIGINS"] = args.ui_cors_origins

    uvicorn.run("master_server.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
