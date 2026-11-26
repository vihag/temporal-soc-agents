"""Every side effect the Workflow needs, as Temporal Activities.

Activities are where I/O and non-determinism live. Temporal records each result in history, retries
failures with the policy in OPTIONS, and never re-runs a completed Activity on replay. Each function
is thin: build a client, call it, return a dataclass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from soc.llm import GrokClient
from soc.models import (
    Alert,
    CaseInput,
    LLMReply,
    LLMRequest,
    QueueMessage,
    ShiftReport,
    SoarReceipt,
    SoarSubmit,
    ToolCall,
)
from soc.scenarios import Scenario
from soc.settings import Settings
from soc.soar import HttpSoarClient
from soc.tools import TOOLS
from soc.visibility import ASSIGNEE, CASE_STATUS

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
async def fetch_alert(inp: CaseInput) -> Alert:
    _maybe_fail(_scenario(inp.scenario), "fetch_alert")
    params = {"scenario": inp.scenario, "alert_id": inp.alert_id}
    async with httpx.AsyncClient(base_url=Settings.from_env().splunk_url, timeout=10) as http:
        response = await http.get("/v1/alert", params=params)
    response.raise_for_status()
    return Alert(**response.json())


@activity.defn
async def llm_create(request: LLMRequest) -> LLMReply:
    """One model turn, then validation. Self-healing: an answer that is not what we asked for
    (missing keys, unknown tool) raises, and Temporal retries the call: a fresh sample."""
    scenario, settings = _scenario(request.scenario), Settings.from_env()
    _maybe_fail(scenario, "llm_create")
    reply = await GrokClient(settings).complete(request)
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


@activity.defn
async def receive_alerts(max_messages: int) -> list[QueueMessage]:
    """Long-poll the alert queue. While the queue is down this fails and Temporal keeps
    retrying (no attempt limit): alerts wait safely in the queue until it recovers."""
    async with httpx.AsyncClient(base_url=Settings.from_env().queue_url, timeout=10) as http:
        response = await http.post("/v1/receive", json={"max": max_messages})
    response.raise_for_status()
    return [QueueMessage(**m) for m in response.json()]


@activity.defn
async def ack_alerts(receipts: list[str]) -> None:
    """Delete messages only after their cases exist, so a crash in between means redelivery,
    and redelivery is a harmless duplicate (case IDs are unique)."""
    async with httpx.AsyncClient(base_url=Settings.from_env().queue_url, timeout=10) as http:
        response = await http.post("/v1/delete", json={"receipts": receipts})
    response.raise_for_status()


@activity.defn
async def shift_report(query: str) -> ShiftReport:
    """Count cases from Visibility (their search attributes). No case Workflow is touched."""
    report, seen = ShiftReport(at=datetime.now(UTC).isoformat()), set()
    async for wf in activity.client().list_workflows(query, limit=1000):
        if wf.id in seen:
            continue  # newest run of a re-used case ID only
        seen.add(wf.id)
        status = wf.typed_search_attributes.get(CASE_STATUS) or "UNINDEXED"
        assignee = wf.typed_search_attributes.get(ASSIGNEE) or "-"
        report.total += 1
        report.by_status[status] = report.by_status.get(status, 0) + 1
        if status not in ("SUBMITTED", "AUTO_RESOLVED", "NEEDS_REVIEW"):
            report.open_by_assignee[assignee] = report.open_by_assignee.get(assignee, 0) + 1
        if status == "ESCALATED":
            report.escalated.append(wf.id)
    return report


def _policy(timeout_s: int, attempts: int, max_interval_s: int = 10) -> dict[str, Any]:
    return {
        "start_to_close_timeout": timedelta(seconds=timeout_s),
        "retry_policy": RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=max_interval_s),
            maximum_attempts=attempts,  # 0 = retry until it works
        ),
    }


# Timeouts and retries for each Activity, in one table (DESIGN.md 6.4).
OPTIONS: dict[str, dict[str, Any]] = {
    "fetch_alert": _policy(10, 10),
    "llm_create": _policy(30, 10),
    "run_tool": _policy(15, 10),
    "notify": _policy(10, 3),
    "soar_submit": _policy(15, 5),
    "receive_alerts": _policy(15, 0, max_interval_s=3),
    "ack_alerts": _policy(15, 0, max_interval_s=3),
    "shift_report": _policy(30, 5),
}


async def call(fn: Callable[[Any], Awaitable[T]], arg: Any) -> T:
    """Workflow-side helper: run Activity `fn` with its row from OPTIONS."""
    return await workflow.execute_activity(fn, arg, **OPTIONS[fn.__name__])
