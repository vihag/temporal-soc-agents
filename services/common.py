"""Shared plumbing for the two mock services: a JSON-only stdlib HTTP handler. No framework,
so the mocks add no dependencies and stay obviously "just a fake upstream"."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class JsonHandler(BaseHTTPRequestHandler):
    """Subclasses implement `get(path, query)` / `post(path, body)` and return (code, json)."""

    def get(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
        return 404, {"error": "not found"}

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return 404, {"error": "not found"}

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._reply(200, {"ok": True})
        self._reply(*self.get(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self._reply(
            *self.post(urlparse(self.path).path, json.loads(self.rfile.read(length) or b"{}"))
        )

    def _reply(self, code: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass  # keep demo output clean


def make_server(handler: type[JsonHandler], port: int) -> ThreadingHTTPServer:
    """Bind to MOCK_HOST (0.0.0.0 inside Compose, 127.0.0.1 by default)."""
    return ThreadingHTTPServer((os.environ.get("MOCK_HOST", "127.0.0.1"), port), handler)


def serve_in_thread(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
