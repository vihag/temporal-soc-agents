"""The five tools agents can call. Each is a small class: a name, a JSON schema the model sees,
and `run()`, which queries Mock Splunk. `activities.run_tool` is the only caller, so every tool
call is a retried, recorded Activity."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx


class Tool:
    name: ClassVar[str]
    description: ClassVar[str]
    params: ClassVar[dict[str, str]] = {}  # argument name -> description (all strings)

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
        async with httpx.AsyncClient(base_url=splunk_url, timeout=10) as http:
            response = await http.get(f"/v1/{self.name}", params={"scenario": scenario, **args})
        response.raise_for_status()  # Temporal retries the Activity on failure
        return dict(response.json())


class ListAlertEvidence(Tool):
    name = "list_alert_evidence"
    description = "List the raw events attached to the alert."


class SearchEvidence(Tool):
    name = "search_evidence"
    description = "Run a Splunk search for events related to the alert."
    params = {"query": "Splunk search, e.g. host=fin-ws-042 process=svc.exe"}


class LookupAsset(Tool):
    name = "lookup_asset"
    description = "Look up a host in the asset inventory (owner, criticality)."
    params = {"host": "Hostname"}


class EnrichIoc(Tool):
    name = "enrich_ioc"
    description = "Get threat-intel reputation for an IP, domain or hash."
    params = {"indicator": "IP, domain or file hash"}


class LookupMitre(Tool):
    name = "lookup_mitre"
    description = "Map an observed behaviour to a MITRE ATT&CK technique."
    params = {"behaviour": "Short description of the behaviour"}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (ListAlertEvidence(), SearchEvidence(), LookupAsset(), EnrichIoc(), LookupMitre())
}
