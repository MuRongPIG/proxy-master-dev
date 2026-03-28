import time
import logging

import requests

from worker_node.checker import check_proxy
from worker_node.gateway import MasterNodeGateway


class DetectorNode:
    def __init__(
        self,
        gateway: MasterNodeGateway,
        poll_interval: float = 2.0,
        timeout: float = 8.0,
        target_url: str = "https://npmjs.org/cdn-cgi/trace",
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.gateway = gateway
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.target_url = target_url
        self.heartbeat_interval = heartbeat_interval

    def run_forever(self) -> None:
        logger = logging.getLogger(__name__)
        logger.info("worker 注册开始: node=%s worker=%s", self.gateway.node_name, self.gateway.worker_id)
        self.gateway.register()
        logger.info("worker 注册成功: node=%s worker=%s", self.gateway.node_name, self.gateway.worker_id)
        last_heartbeat = 0.0
        backoff = self.poll_interval
        while True:
            try:
                now = time.time()
                if now - last_heartbeat >= self.heartbeat_interval:
                    self.gateway.heartbeat_worker()
                    last_heartbeat = now

                task = self.gateway.pull_task()
                if not task:
                    time.sleep(self.poll_interval)
                    continue

                success, latency_ms, response_status, error, country_code = check_proxy(
                    proxy_url=task["proxy_url"],
                    target_url=self.target_url,
                    timeout=self.timeout,
                )
                self.gateway.push_result(task["task_id"], success, latency_ms, response_status, error, country_code)
                logger.info(
                    "任务完成: worker=%s task=%s success=%s latency_ms=%s status=%s country=%s",
                    self.gateway.worker_id,
                    task.get("task_id"),
                    success,
                    latency_ms,
                    response_status,
                    country_code,
                )
                backoff = self.poll_interval
            except requests.RequestException as exc:
                logger.warning("请求异常，进入退避: worker=%s error=%s", self.gateway.worker_id, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:
                logger.exception("worker 循环异常，进入退避: worker=%s", self.gateway.worker_id)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
