"""Shared plumbing for the mock services: a JSON-only stdlib HTTP handler plus a chaos switch.

No framework, so the mocks add no dependencies and stay obviously "just a fake upstream".
Every mock gets `POST /admin/outage {"seconds": N, "code": 503 | 429}`: for N seconds it answers
everything else with that status, so the demo can break a dependency and watch Temporal cope.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class MockServer(ThreadingHTTPServer):
    outage_until: float = 0.0
    outage_code: int = 503
    fail_next: int = 0  # tests: fail exactly this many requests (time-skipping ignores clocks)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):  # a killed worker hung up: fine
            super().handle_error(request, client_address)


class JsonHandler(BaseHTTPRequestHandler):
    """Subclasses implement `get(path, query)` / `post(path, body)` and return (code, json)."""

    server: MockServer
    always_up: tuple[str, ...] = ()  # paths an outage does not touch

    def get(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
        return 404, {"error": "not found"}

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return 404, {"error": "not found"}

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._reply(200, {"ok": True, "down": time.time() < self.server.outage_until})
        if url.path in self.always_up or not self._down():
            self._reply(*self.get(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        if path == "/admin/outage":
            self.server.outage_until = time.time() + float(body.get("seconds", 10))
            self.server.outage_code = int(body.get("code", 503))
            return self._reply(200, {"down_for_s": body.get("seconds", 10)})
        if path in self.always_up or not self._down():
            self._reply(*self.post(path, body))

    def _down(self) -> bool:
        """During an outage, answer with the outage status (429s carry Retry-After)."""
        if self.server.fail_next > 0:
            self.server.fail_next -= 1
        elif time.time() >= self.server.outage_until:
            return False
        code = self.server.outage_code
        self._reply(code, {"error": "injected outage"}, {"Retry-After": "2"} if code == 429 else {})
        return True

    def _reply(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass  # keep demo output clean


def make_server(handler: type[JsonHandler], port: int) -> MockServer:
    """Bind to MOCK_HOST (0.0.0.0 inside Compose, 127.0.0.1 by default)."""
    return MockServer((os.environ.get("MOCK_HOST", "127.0.0.1"), port), handler)


def serve_in_thread(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
