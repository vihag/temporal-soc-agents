"""Mock Grok: an OpenAI-compatible /v1/chat/completions that replays recorded fixture turns.

Run: python -m services.mock_grok  (:8003). Fixture mode points the *real* GrokClient here, so
the HTTP path, error mapping and retries are the same code that talks to xAI. The client tags
each call with X-Scenario / X-Agent / X-Step headers; the reply is that agent's recorded turn.
The `ask` agent picks a recorded answer by keyword in the analyst's question.
"""

from __future__ import annotations

import json
import time
from typing import Any

from services.common import JsonHandler, MockServer, make_server
from soc.scenarios import Scenario
from soc.settings import Settings


def reply(
    scenario: Scenario, agent: str, step: int, messages: list[dict[str, Any]]
) -> dict[str, Any]:
    if agent == "ask":
        prompt = next(m["content"] for m in messages if m["role"] == "user")
        question = prompt.rsplit("Analyst question:", 1)[-1].lower()
        scripts = scenario.data["ask"]
        topic = next((k for k in scripts if k in question), "default")
        turn = scripts[topic][min(step, len(scripts[topic]) - 1)]
    else:
        turn = scenario.model_turn(agent, step)
    turn = scenario.render(turn)
    message: dict[str, Any] = {"role": "assistant", "content": None}
    if "tool" in turn:
        call = {"name": turn["tool"], "arguments": json.dumps(turn.get("args", {}))}
        message["tool_calls"] = [
            {"id": f"call_{agent}_{step}", "type": "function", "function": call}
        ]
    else:
        message["content"] = json.dumps(turn["final"])
    return {
        "id": f"fx-{agent}-{step}",
        "object": "chat.completion",
        "created": 0,
        "model": "grok-fixture",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def grok_server(port: int = 8003) -> MockServer:
    settings = Settings.from_env()

    class Handler(JsonHandler):
        def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
            if path != "/v1/chat/completions":
                return 404, {"error": "not found"}
            time.sleep(settings.fixture_latency_s)
            scenario = Scenario.load(self.headers["X-Scenario"], settings.fixtures_dir)
            step = int(self.headers.get("X-Step", 0))
            return 200, reply(scenario, self.headers["X-Agent"], step, body["messages"])

    return make_server(Handler, port)


if __name__ == "__main__":
    print("Mock Grok listening on :8003")
    grok_server().serve_forever()
