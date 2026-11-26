"""Mock alert queue: a tiny SQS look-alike.  Run: python -m services.mock_queue  (:8004)

`send` enqueues, `receive` hands out up to N messages and hides them for a visibility timeout,
`delete` acknowledges. A message that is received but never deleted (the consumer crashed)
reappears after the timeout, so nothing is lost. During an injected outage `receive` fails
while producers keep sending: alerts pile up and drain when the queue recovers.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

from services.common import JsonHandler, MockServer, make_server

VISIBILITY_S = 30


class AlertQueue:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}  # receipt -> message
        self.hidden_until: dict[str, float] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def send(self, body: dict[str, Any]) -> str:
        with self._lock:
            receipt = f"m-{next(self._ids)}"
            self.messages[receipt] = body
            return receipt

    def receive(self, max_messages: int) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            ready = [r for r in self.messages if self.hidden_until.get(r, 0) <= now][:max_messages]
            for receipt in ready:
                self.hidden_until[receipt] = now + VISIBILITY_S
            return [{**self.messages[r], "receipt": r} for r in ready]

    def delete(self, receipts: list[str]) -> None:
        with self._lock:
            for receipt in receipts:
                self.messages.pop(receipt, None)
                self.hidden_until.pop(receipt, None)


def queue_server(port: int = 8004, queue: AlertQueue | None = None) -> MockServer:
    q = queue or AlertQueue()

    class Handler(JsonHandler):
        # In an outage consumers cannot receive, but producers still enqueue: alerts buffer.
        always_up = ("/v1/send", "/v1/depth")

        def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
            if path == "/v1/send":
                return 200, {"receipt": q.send(body)}
            if path == "/v1/receive":
                return 200, q.receive(int(body.get("max", 10)))
            if path == "/v1/delete":
                q.delete(body["receipts"])
                return 200, {"deleted": len(body["receipts"])}
            return 404, {"error": "not found"}

        def get(self, path: str, query: dict[str, str]) -> tuple[int, Any]:
            down = time.time() < self.server.outage_until
            return 200, {"depth": len(q.messages), "down": down}

    server = make_server(Handler, port)
    server.queue = q  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    print("Mock queue listening on :8004")
    queue_server().serve_forever()
