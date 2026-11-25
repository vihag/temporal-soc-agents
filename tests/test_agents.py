"""Agent behaviour: self-healing model output, real tool use, parallel agents, Grok error mapping.
No test calls a live model: Grok responses are faked at the HTTP layer."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ApplicationError

from soc.llm import GrokClient
from soc.models import CaseInput, LLMRequest, Status
from soc.settings import Settings
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.workflow import CaseInvestigationWorkflow


async def _history(env, wf_id):
    return [e async for e in env.client.get_workflow_handle(wf_id).fetch_history_events()]


async def test_llm_self_heal(env, settings):
    """Two garbled triage answers fail validation; Temporal retries; the third is used."""
    case = await env.client.execute_workflow(
        CaseInvestigationWorkflow.run,
        CaseInput("triage-glitch-01"),
        id="case-SPL-2026-0104",
        task_queue=settings.task_queue,
    )
    assert case.status == Status.AUTO_RESOLVED
    attempts = [
        e.activity_task_started_event_attributes.attempt
        for e in await _history(env, case.case_id)
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
    ]
    assert 3 in attempts


async def test_evidence_loop_uses_tools(env, settings, splunk):
    before = dict(splunk.hits)
    sim = SIMULATIONS["approve_to_submitted"]
    case = await run_simulation(env.client, sim, settings, env.sleep, Narrator(lambda _: None))
    used = {e.source for e in case.evidence if e.agent == "evidence"}
    assert {"search_evidence", "lookup_asset"} <= used
    assert splunk.hits["search_evidence"] > before.get("search_evidence", 0)  # real HTTP call
    assert all(f.evidence_refs for f in case.findings)


async def test_parallel_agents(env, settings):
    """Evidence and Threat start in the same Workflow Task: two llm_create calls scheduled
    back to back, before either completes."""
    sim = SIMULATIONS["approve_to_submitted"]
    case = await run_simulation(env.client, sim, settings, env.sleep, Narrator(lambda _: None))
    agents = {f.agent for f in case.findings}
    assert agents == {"evidence", "threat"}
    kinds = [e.event_type for e in await _history(env, case.case_id)]
    scheduled = EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    assert any(a == b == scheduled for a, b in zip(kinds, kinds[1:], strict=False))


def _grok(handler) -> GrokClient:
    settings = Settings(xai_api_key="test-key", grok_client="http")
    return GrokClient(
        settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


REQUEST = LLMRequest("triage", "auto-fp-01", 0, [{"role": "user", "content": "hi"}], [])


async def test_grok_rate_limit_uses_retry_after():
    client = _grok(lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={}))
    with pytest.raises(ApplicationError) as err:
        await client.complete(REQUEST)
    assert err.value.next_retry_delay == timedelta(seconds=7) and not err.value.non_retryable


async def test_grok_4xx_is_not_retried():
    client = _grok(lambda r: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(ApplicationError) as err:
        await client.complete(REQUEST)
    assert err.value.non_retryable


async def test_grok_parses_tool_call_and_tokens():
    body = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "grok-4.3",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_evidence",
                                "arguments": '{"query": "host=a"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129},
    }
    reply = await _grok(lambda r: httpx.Response(200, json=body)).complete(REQUEST)
    assert (reply.tool, reply.args, reply.tokens) == (
        "search_evidence",
        {"query": "host=a"},
        (120, 9),
    )
