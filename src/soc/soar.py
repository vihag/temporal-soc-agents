"""Client for the SOAR platform (our mock in services/mock_soar.py). SOAR owns idempotency:
it is unique on case_id, so a retried submit returns the original ticket instead of a second one."""

from __future__ import annotations

import httpx

from soc.models import SoarReceipt


class SoarClient:
    """Interface so tests or a real SOAR vendor can slot in without touching the Activity."""

    async def submit(self, case_id: str, action: str, approver: str) -> SoarReceipt:
        raise NotImplementedError


class HttpSoarClient(SoarClient):
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def submit(self, case_id: str, action: str, approver: str) -> SoarReceipt:
        body = {"case_id": case_id, "action": action, "approver": approver}
        headers = {"Idempotency-Key": f"soar-submit-{case_id}"}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as http:
            response = await http.post("/v1/submissions", json=body, headers=headers)
        response.raise_for_status()  # 5xx / connection errors -> Temporal retries the Activity
        data = response.json()
        return SoarReceipt(soar_id=data["soar_id"], created=data["created"])
