"""Every side effect the Workflow needs, as Temporal Activities.

Activities are where I/O and non-determinism live. Temporal records each result in history, retries
failures with the policy in OPTIONS, and never re-runs a completed Activity on replay. Each function
is thin: build a client, call it, return a dataclass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from soc.llm import make_llm_client
from soc.models import Alert, LLMReply, LLMRequest, SoarReceipt, SoarSubmit, ToolCall
from soc.scenarios import Scenario
from soc.settings import Settings
from soc.soar import HttpSoarClient

T = TypeVar("T")

TOOLS = ("list_alert_evidence", "search_evidence", "lookup_asset", "enrich_ioc", "lookup_mitre")


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
    scenario = _scenario(scenario_name)
    _maybe_fail(scenario, "fetch_alert")
    return Alert(**scenario.alert)


@activity.defn
async def llm_create(request: LLMRequest) -> LLMReply:
    """One model turn. The client never retries on its own: Temporal owns retries."""
    _maybe_fail(_scenario(request.scenario), "llm_create")
    reply = await make_llm_client(Settings.from_env()).complete(request)
    activity.logger.info(
        "llm_create agent=%s step=%s model=%s", request.agent, request.step, reply.model
    )
    return reply


@activity.defn
async def run_tool(call: ToolCall) -> dict[str, Any]:
    """Dispatch one of the five named tools. In T1 they answer from the scenario fixture."""
    if call.name not in TOOLS:
        raise ApplicationError(f"unknown tool {call.name!r}", non_retryable=True)
    scenario = _scenario(call.scenario)
    _maybe_fail(scenario, f"run_tool:{call.name}")
    return scenario.tool_result(call.name)


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
