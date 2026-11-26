"""Seed alerts and break things on purpose. Used by `soc seed` / `soc chaos` and the UI panel.

Every mock exposes the same outage switch, so "Splunk down for 20s" is one HTTP call. What
happens next is all Temporal: Activities retry with backoff, Workflows wait, nothing is lost.
"""

from __future__ import annotations

import httpx

from soc.settings import Settings

# Eight alerts for the runbook (alert id = f"{prefix}-{suffix}"). The last repeats the first:
# intake will show it as a duplicate.
SEED = [
    ("1001", "hitl-confirm-01"),  # credential dumping -> investigate -> approve
    ("1002", "auto-fp-01"),  # benign automation -> auto-resolved
    ("1003", "travel-01"),  # impossible travel -> ask agent about exemptions
    ("1004", "hitl-flaky-01"),  # flaky Splunk + lost SOAR reply -> retries, one ticket
    ("1005", "triage-lowconf-01"),  # model unsure -> needs review
    ("1006", "usb-01"),  # USB exfiltration -> reassign / park
    ("1007", "auto-fp-01"),  # benign automation -> auto-resolved
    ("1001", "hitl-confirm-01"),  # duplicate of the first alert -> no-op
]


class Chaos:
    def __init__(self, settings: Settings) -> None:
        self.targets = {
            "queue": settings.queue_url,
            "splunk": settings.splunk_url,
            "grok": settings.mock_grok_url.removesuffix("/v1"),
            "soar": settings.soar_url,
        }
        self.queue_url = settings.queue_url

    async def outage(self, target: str, seconds: float, code: int = 503) -> str:
        """`code=429` makes Grok rate-limit (with Retry-After) instead of failing."""
        async with httpx.AsyncClient(timeout=5) as http:
            response = await http.post(
                f"{self.targets[target]}/admin/outage", json={"seconds": seconds, "code": code}
            )
        response.raise_for_status()
        return f"{target} returns {code} for {seconds:g}s"

    async def push(self, alert_id: str, scenario: str, approval_timeout_s: int = 120) -> None:
        body = {
            "alert_id": alert_id,
            "scenario": scenario,
            "approval_timeout_s": approval_timeout_s,
        }
        async with httpx.AsyncClient(timeout=5) as http:
            (await http.post(f"{self.queue_url}/v1/send", json=body)).raise_for_status()

    async def seed(self, prefix: str = "SPL-2026") -> int:
        for suffix, scenario in SEED:
            await self.push(f"{prefix}-{suffix}", scenario)
        return len(SEED)

    async def sla_breach(self, alert_id: str) -> None:
        """A credential-dumping alert with a 20-second approval SLA: leave it and it escalates."""
        await self.push(alert_id, "hitl-confirm-01", approval_timeout_s=20)
