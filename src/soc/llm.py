"""Model clients. Only the `llm_create` Activity uses these, so every model call is a durable,
retryable, recorded step in Workflow history."""

from __future__ import annotations

from temporalio.exceptions import ApplicationError

from soc.models import LLMReply, LLMRequest
from soc.scenarios import Scenario
from soc.settings import Settings


class LLMClient:
    """Interface: one Chat Completions turn in, one reply (answer or tool call) out."""

    async def complete(self, request: LLMRequest) -> LLMReply:
        raise NotImplementedError


class FixtureLLMClient(LLMClient):
    """Replays the scenario's recorded replies. Zero cost; the default in tests, CI and demos."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(self, request: LLMRequest) -> LLMReply:
        turn = Scenario.load(request.scenario, self.settings.fixtures_dir).model_turn(
            request.agent, request.step
        )
        if "tool" in turn:
            return LLMReply(model="fixture", tool=turn["tool"], args=turn.get("args", {}))
        return LLMReply(model="fixture", content=turn["final"])


def make_llm_client(settings: Settings) -> LLMClient:
    if settings.grok_client == "fixture":
        return FixtureLLMClient(settings)
    raise ApplicationError(
        "Live Grok client lands in T2; use GROK_CLIENT=fixture", non_retryable=True
    )
