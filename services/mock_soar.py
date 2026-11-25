"""Mock SOAR: a tiny HTTP service that files containment tickets, unique on case_id.

Run: python -m services.mock_soar  (listens on :8002). Stdlib only, so it needs no framework.
The uniqueness rule is the second half of exactly-once: Temporal may retry `soar_submit`
(for example when the response is lost), and this service makes the retry harmless.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


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
            ticket = {
                "soar_id": f"SOAR-{case_id.removeprefix('case-')}",
                "case_id": case_id,
                "action": action,
                "approver": approver,
            }
            self.tickets[case_id] = ticket
            return 201, {**ticket, "created": True}


def make_server(port: int = 8002, store: SoarStore | None = None) -> ThreadingHTTPServer:
    soar = store or SoarStore()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: Any) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path != "/v1/submissions":
                return self._reply(404, {"error": "not found"})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._reply(*soar.submit(body["case_id"], body["action"], body["approver"]))

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._reply(200, {"ok": True})
            case_id = parse_qs(url.query).get("case_id", [""])[0]
            tickets = [t for t in soar.tickets.values() if not case_id or t["case_id"] == case_id]
            self._reply(200, tickets)

        def log_message(self, *args: Any) -> None:
            pass  # keep demo output clean

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = soar  # type: ignore[attr-defined]
    return server


def serve_in_thread(port: int = 8002) -> ThreadingHTTPServer:
    server = make_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    print("Mock SOAR listening on http://127.0.0.1:8002")
    make_server().serve_forever()
