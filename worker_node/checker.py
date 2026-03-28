import time
from typing import Optional, Tuple

import requests


def _extract_loc_country(trace_text: str) -> Optional[str]:
    for line in trace_text.splitlines():
        if not line.startswith("loc="):
            continue
        code = line[4:].strip().upper()
        if len(code) == 2 and code.isalpha():
            return code
    return None


def check_proxy(
    proxy_url: str,
    target_url: str,
    timeout: float,
) -> Tuple[bool, Optional[int], Optional[int], Optional[str], Optional[str]]:
    """检测代理是否可访问目标地址。"""
    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.perf_counter()

    try:
        response = requests.get(target_url, proxies=proxies, timeout=timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        country_code = _extract_loc_country(response.text)
        if 200 <= response.status_code < 400:
            return True, latency_ms, response.status_code, None, country_code
        return False, latency_ms, response.status_code, f"Unexpected status code: {response.status_code}", country_code
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return False, latency_ms, None, str(exc), None
