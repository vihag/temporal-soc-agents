"""Mock Splunk: serves alerts and tool lookups from fixtures/scenarios/*.json.

Run: python -m services.mock_splunk  (listens on :8001). Read-only, like the real thing would be
for these agents. It counts requests so tests can prove an agent really called a tool.
"""

from __future__ import annotations

from collections import Counter
from http.server import ThreadingHTTPServer
from typing import Any

from services.common import JsonHandler, make_server
from soc.scenarios import Scenario
from soc.settings import Settings

ENDPOINTS = ("list_alert_evidence", "search_evidence", "lookup_asset", "enrich_ioc", "lookup_mitre")


def splunk_server(port: int = 8001) -> ThreadingHTTPServer:
    fixtures = Settings.from_env().fixtures_dir
    hits: Counter[str] = Counter()

    class Handler(JsonHandler):
        def get(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
            name = path.removeprefix("/v1/")
            hits[name] += 1
            scenario = Scenario.load(query["scenario"], fixtures)
            if name == "alert":
                return 200, scenario.alert
            if name in ENDPOINTS and name in scenario.data.get("tools", {}):
                return 200, scenario.tool_result(name)
            return 404, {"error": f"unknown endpoint {name}"}

    server = make_server(Handler, port)
    server.hits = hits  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    print("Mock Splunk listening on :8001")
    splunk_server().serve_forever()
