"""The seven tools agents can call: a name, a JSON schema the model sees, and a lookup against
Mock Splunk. `activities.run_tool` is the only caller, so every tool call is a retried, recorded
Activity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from soc.upstream import request_json


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict[str, str] = field(default_factory=dict)  # argument name -> description (strings)

    def schema(self) -> dict[str, Any]:
        """OpenAI-compatible function-tool schema (what Grok sees)."""
        properties = {k: {"type": "string", "description": v} for k, v in self.params.items()}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties},
            },
        }

    async def run(self, splunk_url: str, scenario: str, args: dict[str, Any]) -> dict[str, Any]:
        params = {"scenario": scenario, **args}
        return dict(await request_json("GET", splunk_url, f"/v1/{self.name}", params=params))


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool("list_alert_evidence", "List the raw events attached to the alert."),
        Tool(
            "search_evidence",
            "Run a Splunk search for events related to the alert.",
            {"query": "Splunk search, e.g. host=fin-ws-042 process=svc.exe"},
        ),
        Tool(
            "lookup_asset",
            "Look up a host in the asset inventory (owner, criticality).",
            {"host": "Hostname"},
        ),
        Tool(
            "enrich_ioc",
            "Get threat-intel reputation for an IP, domain or hash.",
            {"indicator": "IP, domain or file hash"},
        ),
        Tool(
            "lookup_mitre",
            "Map an observed behaviour to a MITRE ATT&CK technique.",
            {"behaviour": "Short description of the behaviour"},
        ),
        Tool(
            "search_logins",
            "Splunk login events for a host and user over the last N days.",
            {"host": "Hostname", "user": "Username", "days": "How many days back"},
        ),
        Tool(
            "lookup_exemptions",
            "Exemption tickets (approved out-of-policy use) for a user in a time window.",
            {"user": "Username", "window": "e.g. this week"},
        ),
    )
}
