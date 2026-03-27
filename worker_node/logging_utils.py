import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler


def _parse_level(level: str) -> int:
    value = (level or "INFO").strip().upper()
    return getattr(logging, value, logging.INFO)


def _cleanup_old_logs(log_dir: str, retention_days: int) -> None:
    if retention_days < 1:
        retention_days = 1
    cutoff = time.time() - retention_days * 86400
    for name in os.listdir(log_dir):
        if not name.endswith(".log") and ".log." not in name:
            continue
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


def setup_worker_logging(log_dir: str, level: str = "INFO", retention_days: int = 14) -> str:
    os.makedirs(log_dir, exist_ok=True)
    _cleanup_old_logs(log_dir=log_dir, retention_days=retention_days)

    root = logging.getLogger()
    root.setLevel(_parse_level(level))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "worker.log"),
        when="midnight",
        interval=1,
        backupCount=max(1, retention_days),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info("worker 日志系统已启用，目录=%s，保留天数=%s", log_dir, retention_days)
    return log_dir
