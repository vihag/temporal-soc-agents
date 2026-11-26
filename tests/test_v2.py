"""v2 primitives on the time-skipping server: queue intake (child Workflows, duplicate no-op,
outage retries, continue-as-new), reassign, park timers, the ask-agent child Workflow, and a
model outage that heals."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from soc.intake import IntakeWorkflow
from soc.models import (
    AskRequest,
    CaseInput,
    DecisionRequest,
    IntakeState,
    ParkRequest,
    ReassignRequest,
    Status,
)
from soc.worker import build_worker
from soc.workflow import CaseInvestigationWorkflow as CaseWF


@pytest_asyncio.fixture
async def intake_env(settings):
    """A throwaway server for the endless intake loop, so its timers never leak into others."""
    async with await WorkflowEnvironment.start_time_skipping() as env:  # noqa: SIM117
        async with build_worker(env.client, settings.task_queue):
            yield env


async def _until(env, handle: WorkflowHandle, done, query=CaseWF.get_case, step: float = 0.5):
    for _ in range(600):
        value = await handle.query(query)
        if done(value):
            return value
        await env.sleep(step)
    raise AssertionError("condition never met")


async def _attempts(handle: WorkflowHandle, activity: str) -> list[int]:
    names: dict[int, str] = {}
    attempts = []
    async for e in handle.fetch_history_events():
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            names[e.event_id] = e.activity_task_scheduled_event_attributes.activity_type.name
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            a = e.activity_task_started_event_attributes
            if names[a.scheduled_event_id] == activity:
                attempts.append(a.attempt)
    return attempts


async def _proposed_case(env, settings, wf_id: str, scenario: str = "hitl-confirm-01"):
    handle = await env.client.start_workflow(
        CaseWF.run, CaseInput(scenario), id=wf_id, task_queue=settings.task_queue
    )
    await _until(env, handle, lambda c: c.status == Status.DISPOSITION_PROPOSED)
    return handle


async def test_intake_round_robin_duplicate_and_queue_outage(intake_env, settings, queue):
    env = intake_env
    queue.fail_next = 3  # the queue is down for intake's first three polls
    url = f"http://127.0.0.1:{queue.server_port}/v1/send"
    async with httpx.AsyncClient() as http:  # producers still enqueue during the outage
        for n in (1, 2, 3, 1):  # the 4th message duplicates the 1st
            body = {"alert_id": f"SPL-T-00{n}", "scenario": "auto-fp-01"}
            (await http.post(url, json=body)).raise_for_status()
    intake = await env.client.start_workflow(
        IntakeWorkflow.run,
        IntakeState(poll_interval_s=1),
        id="intake-test",
        task_queue=settings.task_queue,
    )
    stats = await _until(env, intake, lambda s: s.started + s.duplicates == 4, IntakeWorkflow.stats)
    assert (stats.started, stats.duplicates) == (3, 1)
    assert any("duplicate SPL-T-001 ignored" in line for line in stats.recent)
    assignees = [
        (await env.client.get_workflow_handle(f"case-SPL-T-00{n}").query(CaseWF.get_case)).assignee
        for n in (1, 2, 3)
    ]
    assert assignees == ["vihag", "aisha", "ben"]  # round robin
    assert not queue.queue.messages  # everything acknowledged; nothing lost in the outage
    assert max(await _attempts(intake, "receive_alerts")) > 1  # Temporal retried through it
    await intake.terminate("test done")


async def test_intake_continues_as_new_and_keeps_counters(intake_env, settings):
    env = intake_env
    state = IntakeState(polls_per_run=2, poll_interval_s=1, next_analyst=5)
    intake = await env.client.start_workflow(
        IntakeWorkflow.run, state, id="intake-can-test", task_queue=settings.task_queue
    )
    first_run = intake.result_run_id
    for _ in range(100):
        if (await intake.describe()).run_id != first_run:
            break
        await env.sleep(1)
    assert (await intake.describe()).run_id != first_run
    assert (await intake.query(IntakeWorkflow.stats)).next_analyst == 5
    await intake.terminate("test done")


async def test_reassign_moves_the_case_to_the_new_analyst(env, settings):
    handle = await _proposed_case(env, settings, "case-v2-reassign")
    case = await handle.execute_update(
        CaseWF.reassign, ReassignRequest("aisha", "vihag", "urgent leave")
    )
    assert case.assignee == "aisha" and case.status == Status.DISPOSITION_PROPOSED
    with pytest.raises(WorkflowUpdateFailedError):  # vihag no longer owns it
        await handle.execute_update(CaseWF.decide, DecisionRequest("approve", "vihag"))
    await handle.execute_update(CaseWF.decide, DecisionRequest("approve", "aisha"))
    case = await handle.result()
    assert case.status == Status.SUBMITTED and any("Reassigned" in t.note for t in case.timeline)


async def test_park_timer_resumes_and_sla_does_not_run_while_parked(env, settings):
    handle = await _proposed_case(env, settings, "case-v2-park")
    case = await handle.execute_update(CaseWF.park, ParkRequest(3600, "vihag"))
    assert case.status == Status.PARKED
    for _ in range(10):  # 10 minutes, far past the 120s approval SLA: parked, not escalated
        await env.sleep(60)
        assert (await handle.query(CaseWF.get_case)).status == Status.PARKED
    await _until(env, handle, lambda c: c.status == Status.DISPOSITION_PROPOSED, step=60)
    await handle.execute_update(CaseWF.park, ParkRequest(600, "vihag"))
    case = await handle.execute_update(CaseWF.unpark, "vihag")
    assert case.status == Status.DISPOSITION_PROPOSED
    await handle.execute_update(CaseWF.decide, DecisionRequest("approve", "vihag"))
    statuses = [t.status for t in (await handle.result()).timeline]
    assert statuses.count(Status.PARKED) == 2 and Status.ESCALATED not in statuses


async def test_ask_agent_runs_as_child_and_attaches_findings(env, settings):
    handle = await _proposed_case(env, settings, "case-v2-ask")
    question = await handle.execute_update(
        CaseWF.ask, AskRequest("Any exemption tickets this week for this user?", "vihag")
    )
    assert "exemption" in question.answer
    child = await env.client.get_workflow_handle(question.workflow_id).describe()
    assert child.status and child.status.name == "COMPLETED"
    case = await handle.query(CaseWF.get_case)
    assert any(f.agent == "ask" for f in case.findings)
    assert any(e.source == "lookup_exemptions" for e in case.evidence)
    await handle.execute_update(CaseWF.decide, DecisionRequest("approve", "vihag"))
    assert (await handle.result()).status == Status.SUBMITTED


async def test_model_outage_heals(env, settings, grok):
    grok.fail_next, grok.outage_code = 3, 503  # three 503s, then healthy
    case = await env.client.execute_workflow(
        CaseWF.run,
        CaseInput("auto-fp-01", alert_id="SPL-T-GROK"),
        id="case-v2-grok",
        task_queue=settings.task_queue,
    )
    assert (
        case.status == Status.AUTO_RESOLVED and case.alert and case.alert.alert_id == "SPL-T-GROK"
    )
    handle = env.client.get_workflow_handle("case-v2-grok")
    assert max(await _attempts(handle, "llm_create")) == 4
