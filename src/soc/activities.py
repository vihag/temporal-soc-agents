"""Every side effect the Workflow needs, as Temporal Activities.

Activities are where I/O and non-determinism live. Temporal records each result in history, retries
failures with the policy in OPTIONS, and never re-runs a completed Activity on replay. Each function
is thin: build a client, call it, return a dataclass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

import httpx
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from soc.llm import make_llm_client
from soc.models import Alert, LLMReply, LLMRequest, SoarReceipt, SoarSubmit, ToolCall
from soc.scenarios import Scenario
from soc.settings import Settings
from soc.soar import HttpSoarClient
from soc.tools import TOOLS

T = TypeVar("T")


def _scenario(name: str) -> Scenario:
    return Scenario.load(name, Settings.from_env().fixtures_dir)


def _maybe_fail(scenario: Scenario, name: str) -> None:
    """Fixture fault injection, so the demo can show Temporal retrying a real failure."""
    fault, attempt = scenario.fault(name), activity.info().attempt
    if fault == "fatal":
        raise ApplicationError(f"{name}: upstream rejected the request (400)", non_retryable=True)
    if isinstance(fault, int) and attempt <= fault:
        raise RuntimeError(f"{name}: upstream timed out (injected fault, attempt {attempt})")


@activity.defn
async def fetch_alert(scenario_name: str) -> Alert:
    _maybe_fail(_scenario(scenario_name), "fetch_alert")
    async with httpx.AsyncClient(base_url=Settings.from_env().splunk_url, timeout=10) as http:
        response = await http.get("/v1/alert", params={"scenario": scenario_name})
    response.raise_for_status()
    return Alert(**response.json())


@activity.defn
async def llm_create(request: LLMRequest) -> LLMReply:
    """One model turn, then validation. Self-healing: an answer that is not what we asked for
    (missing keys, unknown tool) raises, and Temporal retries the call: a fresh sample."""
    scenario, settings = _scenario(request.scenario), Settings.from_env()
    _maybe_fail(scenario, "llm_create")
    reply = await make_llm_client(settings).complete(request)
    garble = scenario.fault(f"llm_invalid:{request.agent}")
    if isinstance(garble, int) and activity.info().attempt <= garble:  # simulated model glitch
        reply = LLMReply(reply.model, content={"unparsed": "Sure! Here is my analysis..."})
    activity.logger.info(
        "llm_create agent=%s step=%s model=%s (requested %s) tokens=%s",
        request.agent,
        request.step,
        reply.model,
        settings.xai_model,
        reply.tokens,
    )
    if reply.tool is not None and reply.tool not in request.tools:
        raise ApplicationError(f"model called unavailable tool {reply.tool!r}; retrying")
    missing = set(request.answer_keys) - set(reply.content or {})
    if reply.tool is None and missing:
        raise ApplicationError(f"model answer missing {sorted(missing)}; retrying")
    return reply


@activity.defn
async def run_tool(call: ToolCall) -> dict[str, Any]:
    """Run one of the five named tools against Mock Splunk."""
    if call.name not in TOOLS:
        raise ApplicationError(f"unknown tool {call.name!r}", non_retryable=True)
    _maybe_fail(_scenario(call.scenario), f"run_tool:{call.name}")
    return await TOOLS[call.name].run(Settings.from_env().splunk_url, call.scenario, call.args)


@activity.defn
async def notify(message: str) -> None:
    """Page the on-call analyst. In T1 a log line is the pager."""
    activity.logger.info("NOTIFY %s", message)


@activity.defn
async def soar_submit(request: SoarSubmit) -> SoarReceipt:
    """File the containment ticket. Exactly-once = Temporal retries + SOAR unique on case_id.

    The injected fault fires *after* the ticket is filed, as if the response were lost. The retry
    then gets the same ticket back with created=False instead of filing a second one.
    """
    soar = HttpSoarClient(Settings.from_env().soar_url)
    receipt = await soar.submit(request.case_id, request.action, request.approver)
    _maybe_fail(_scenario(request.scenario), "soar_submit")
    return receipt


def _policy(timeout_s: int, attempts: int) -> dict[str, Any]:
    return {
        "start_to_close_timeout": timedelta(seconds=timeout_s),
        "retry_policy": RetryPolicy(
            initial_interval=timedelta(seconds=1), maximum_attempts=attempts
        ),
    }


# Timeouts and retries for each Activity, in one table (DESIGN.md 6.4).
OPTIONS: dict[str, dict[str, Any]] = {
    "fetch_alert": _policy(10, 3),
    "llm_create": _policy(45, 5),
    "run_tool": _policy(15, 3),
    "notify": _policy(10, 3),
    "soar_submit": _policy(15, 5),
}


async def call(fn: Callable[[Any], Awaitable[T]], arg: Any) -> T:
    """Workflow-side helper: run Activity `fn` with its row from OPTIONS."""
    return await workflow.execute_activity(fn, arg, **OPTIONS[fn.__name__])
