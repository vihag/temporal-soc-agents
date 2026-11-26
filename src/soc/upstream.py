"""One way to call the mock upstreams (Splunk, SOAR, queue, outage switch): JSON in, JSON out.

A non-2xx answer raises, which is what an Activity wants: Temporal then applies its retry policy.
"""

from __future__ import annotations

from typing import Any

import httpx


async def request_json(
    method: str, base_url: str, path: str, *, timeout: float = 10, **kwargs: Any
) -> Any:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as http:
        response = await http.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()
