"""Mock SOAR: files containment tickets, unique on case_id.  Run: python -m services.mock_soar

The uniqueness rule is the second half of exactly-once: Temporal may retry `soar_submit`
(for example when the response is lost), and this service makes the retry harmless.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from typing import Any

from services.common import JsonHandler, make_server


class SoarStore:
    """In-memory tickets keyed by case_id."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, case_id: str, action: str, approver: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if existing := self.tickets.get(case_id):
                if existing["action"] != action:
                    return 409, {"error": "case already submitted with a different action"}
                return 200, {**existing, "created": False}
            soar_id = f"SOAR-{case_id.removeprefix('case-')}"
            ticket = {
                "soar_id": soar_id,
                "case_id": case_id,
                "action": action,
                "approver": approver,
            }
            self.tickets[case_id] = ticket
            return 201, {**ticket, "created": True}


def soar_server(port: int = 8002, store: SoarStore | None = None) -> ThreadingHTTPServer:
    soar = store or SoarStore()

    class Handler(JsonHandler):
        def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
            if path != "/v1/submissions":
                return 404, {"error": "not found"}
            return soar.submit(body["case_id"], body["action"], body["approver"])

        def get(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
            case_id = query.get("case_id")
            return 200, [t for t in soar.tickets.values() if case_id in (None, t["case_id"])]

    server = make_server(Handler, port)
    server.store = soar  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    print("Mock SOAR listening on :8002")
    soar_server().serve_forever()
