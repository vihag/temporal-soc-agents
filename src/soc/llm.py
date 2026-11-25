"""Model clients. Only the `llm_create` Activity uses these, so every model call is a durable,
retryable, recorded step in Workflow history."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import openai
from temporalio.exceptions import ApplicationError

from soc.models import LLMReply, LLMRequest
from soc.scenarios import Scenario
from soc.settings import Settings
from soc.tools import TOOLS


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
            call_id = f"call_{request.agent}_{request.step}"
            return LLMReply(
                "fixture", tool=turn["tool"], args=turn.get("args", {}), call_id=call_id
            )
        return LLMReply("fixture", content=turn["final"])


class GrokClient(LLMClient):
    """xAI Grok over the OpenAI-compatible Chat Completions API.

    `max_retries=0`: Temporal owns retries, so the SDK must not retry behind its back. Errors are
    translated into what Temporal should do: 429 -> retry after Retry-After, other 4xx -> give up
    (non-retryable), 5xx / network -> plain exception, so the Activity retry policy applies.
    """

    def __init__(self, settings: Settings, http_client: Any = None) -> None:
        self.settings = settings
        self.client = openai.AsyncOpenAI(
            api_key=settings.xai_api_key,
            base_url=settings.xai_base_url,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, request: LLMRequest) -> LLMReply:
        tools = [TOOLS[name].schema() for name in request.tools]
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.xai_model,
                messages=request.messages,  # type: ignore[arg-type]
                tools=tools or openai.omit,  # type: ignore[arg-type]
                max_tokens=self.settings.llm_max_tokens,
                extra_body={"reasoning_effort": "none"},
            )
        except openai.RateLimitError as err:
            delay = int(err.response.headers.get("retry-after", "5"))
            raise ApplicationError(
                "rate limited", next_retry_delay=timedelta(seconds=delay)
            ) from err
        except openai.APIStatusError as err:
            if err.status_code < 500:
                raise ApplicationError(
                    f"model rejected request: {err.status_code}", non_retryable=True
                ) from err
            raise
        message, usage = response.choices[0].message, response.usage
        tokens = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        if message.tool_calls:
            call = message.tool_calls[0]
            fn = call.function  # type: ignore[union-attr]
            return LLMReply(
                response.model,
                tool=fn.name,
                args=json.loads(fn.arguments or "{}"),
                call_id=call.id,
                tokens=tokens,
            )
        try:
            content = json.loads(message.content or "")
        except json.JSONDecodeError:
            content = {"unparsed": message.content}  # validation in llm_create triggers a retry
        return LLMReply(response.model, content=content, tokens=tokens)


def make_llm_client(settings: Settings) -> LLMClient:
    if settings.grok_client == "http" and settings.xai_api_key:
        return GrokClient(settings)
    return FixtureLLMClient(settings)
