from typing import Any, Optional

import requests


class MasterNodeGateway:
    """封装节点与主服务器的交互，隐藏具体 API 细节。"""

    def __init__(
        self,
        master_url: str,
        node_name: str,
        worker_id: str,
        node_token: str,
        request_timeout: float = 10.0,
    ) -> None:
        self.node_name = node_name
        self.worker_id = worker_id
        self.node_token = node_token
        self.request_timeout = request_timeout
        self.session = requests.Session()
        
        # 始终确保 master_url 包含 'http' scheme，以防用户的配置漏了 'http://'
        master_url = master_url.rstrip("/")
        if not master_url.startswith("http://") and not master_url.startswith("https://"):
            master_url = f"http://{master_url}"
        
        self.master_url = master_url

        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "X-Node-Token": self.node_token,
            }
        )

    def pull_task(self) -> Optional[dict[str, Any]]:
        response = self.session.post(
            f"{self.master_url}/node/pull-task",
            json={"node_name": self.node_name, "worker_id": self.worker_id},
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("found"):
            return None
        return payload.get("task")

    def register(self) -> None:
        response = self.session.post(
            f"{self.master_url}/nodes/register",
            json={"node_name": self.node_name, "worker_id": self.worker_id},
            timeout=self.request_timeout,
        )
        response.raise_for_status()

    def allocate_node_name(self, country_code: str) -> str:
        response = self.session.post(
            f"{self.master_url}/nodes/allocate-name",
            json={"country_code": country_code},
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        node_name = payload.get("node_name")
        if not isinstance(node_name, str) or not node_name:
            raise ValueError("主服务器返回的 node_name 无效")
        return node_name

    def heartbeat_worker(self) -> None:
        response = self.session.post(
            f"{self.master_url}/nodes/worker-heartbeat",
            json={"node_name": self.node_name, "worker_id": self.worker_id},
            timeout=self.request_timeout,
        )
        response.raise_for_status()

    def push_result(
        self,
        task_id: str,
        success: bool,
        latency_ms: Optional[int],
        response_status: Optional[int],
        error: Optional[str],
    ) -> None:
        response = self.session.post(
            f"{self.master_url}/node/push-result/{task_id}",
            json={
                "node_name": self.node_name,
                "worker_id": self.worker_id,
                "success": success,
                "latency_ms": latency_ms,
                "response_status": response_status,
                "error": error,
            },
            timeout=self.request_timeout,
        )
        response.raise_for_status()
