"""The model client. Only the `llm_create` Activity uses it, so every model call is a durable,
retryable, recorded step in Workflow history.

Fixture mode is the same client pointed at Mock Grok (services/mock_grok.py) instead of xAI:
same HTTP path, same error mapping, zero cost.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import openai
from temporalio.exceptions import ApplicationError

from soc.models import LLMReply, LLMRequest
from soc.settings import Settings
from soc.tools import TOOLS


class GrokClient:
    """xAI Grok over the OpenAI-compatible Chat Completions API.

    `max_retries=0`: Temporal owns retries, so the SDK must not retry behind its back. Errors are
    translated into what Temporal should do: 429 -> retry after Retry-After, other 4xx -> give up
    (non-retryable), 5xx / network -> plain exception, so the Activity retry policy applies.
    """

    def __init__(self, settings: Settings, http_client: Any = None) -> None:
        live = settings.grok_client == "http" and bool(settings.xai_api_key)
        self.model = settings.xai_model
        self.max_tokens = settings.llm_max_tokens
        self.client = openai.AsyncOpenAI(
            api_key=settings.xai_api_key if live else "fixture",
            base_url=settings.xai_base_url if live else settings.mock_grok_url,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, request: LLMRequest) -> LLMReply:
        tools = [TOOLS[name].schema() for name in request.tools]
        try:
            async with self.client as client:  # one call per Activity attempt; close the pool
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=request.messages,  # type: ignore[arg-type]
                    tools=tools or openai.omit,  # type: ignore[arg-type]
                    max_tokens=self.max_tokens,
                    extra_body={"reasoning_effort": "none"},
                    # Lets Mock Grok replay the right recorded turn; harmless to xAI.
                    extra_headers={
                        "X-Scenario": request.scenario,
                        "X-Agent": request.agent,
                        "X-Step": str(request.step),
                    },
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
            args = json.loads(fn.arguments or "{}")
            return LLMReply(response.model, tool=fn.name, args=args, call_id=call.id, tokens=tokens)
        try:
            content = json.loads(message.content or "")
        except json.JSONDecodeError:
            content = {"unparsed": message.content}  # validation in llm_create triggers a retry
        return LLMReply(response.model, content=content, tokens=tokens)
