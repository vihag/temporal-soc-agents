"""Client for the SOAR platform (our mock in services/mock_soar.py). SOAR owns idempotency:
it is unique on case_id, so a retried submit returns the original ticket instead of a second one."""

from __future__ import annotations

from soc.models import SoarReceipt
from soc.upstream import request_json


async def submit_ticket(base_url: str, case_id: str, action: str, approver: str) -> SoarReceipt:
    body = {"case_id": case_id, "action": action, "approver": approver}
    headers = {"Idempotency-Key": f"soar-submit-{case_id}"}
    # 5xx / connection errors raise, so Temporal retries the Activity.
    data = await request_json("POST", base_url, "/v1/submissions", json=body, headers=headers)
    return SoarReceipt(soar_id=data["soar_id"], created=data["created"])
