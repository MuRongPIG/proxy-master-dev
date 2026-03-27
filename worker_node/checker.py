import time
from typing import Optional, Tuple

import requests


def check_proxy(proxy_url: str, target_url: str, timeout: float) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
    """检测代理是否可访问目标地址。"""
    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.perf_counter()

    try:
        response = requests.get(target_url, proxies=proxies, timeout=timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if 200 <= response.status_code < 400:
            return True, latency_ms, response.status_code, None
        return False, latency_ms, response.status_code, f"Unexpected status code: {response.status_code}"
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return False, latency_ms, None, str(exc)
