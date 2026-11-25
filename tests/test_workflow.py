"""Edges the simulations do not cover: routing to review, a fatal model error, duplicate case
IDs, Update validation, deterministic replay, and a worker restart mid-case."""

from __future__ import annotations

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Replayer, Worker

from soc.models import CaseInput, DecisionRequest, Status
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.worker import ACTIVITIES, build_worker
from soc.workflow import CaseInvestigationWorkflow


async def _run(env, settings, scenario: str, wf_id: str):
    return await env.client.execute_workflow(
        CaseInvestigationWorkflow.run, CaseInput(scenario), id=wf_id, task_queue=settings.task_queue
    )


async def test_low_confidence_goes_to_review(env, settings):
    case = await _run(env, settings, "triage-lowconf-01", "case-SPL-2026-0102")
    assert case.status == Status.NEEDS_REVIEW and case.route == "review"


async def test_fatal_model_error_goes_to_review(env, settings):
    case = await _run(env, settings, "model-fatal-01", "case-SPL-2026-0103")
    assert (
        case.status == Status.NEEDS_REVIEW and "Triage model call failed" in case.timeline[-1].note
    )


async def test_duplicate_case_id_and_decide_validation(env, settings):
    start = dict(id="case-dup", task_queue=settings.task_queue)
    handle = await env.client.start_workflow(
        CaseInvestigationWorkflow.run, CaseInput("hitl-confirm-01"), **start
    )
    with pytest.raises(WorkflowAlreadyStartedError):
        await env.client.start_workflow(
            CaseInvestigationWorkflow.run, CaseInput("hitl-confirm-01"), **start
        )
    with pytest.raises(WorkflowUpdateFailedError):  # not an analyst
        await handle.execute_update(
            CaseInvestigationWorkflow.decide, DecisionRequest("approve", "bot", actor_type="agent")
        )
    await handle.terminate("test done")


async def test_replay_is_deterministic(env, settings):
    case = await run_simulation(
        env.client,
        SIMULATIONS["reject_then_reinvestigate"].__class__(
            "replay", "hitl-confirm-01", "", ["reject", "approve"]
        ),
        settings,
        env.sleep,
        Narrator(lambda _: None),
    )
    history = await env.client.get_workflow_handle(case.case_id).fetch_history()
    await Replayer(workflows=[CaseInvestigationWorkflow]).replay_workflow(history)


async def test_worker_restart_resumes_without_rerunning_activities(env, settings):
    """C1: stop the only worker while the case waits for approval, start a fresh one, approve.
    The case completes and no finished Activity ran twice (same count as an unbroken run)."""
    queue = f"{settings.task_queue}-restart"
    handle = await env.client.start_workflow(
        CaseInvestigationWorkflow.run,
        CaseInput("hitl-confirm-01"),
        id="case-restart",
        task_queue=queue,
    )
    # Worker #1 caches nothing, so every task it runs is a replay from history. That also keeps
    # the time-skipping test server from routing the next task to its (dead) sticky queue.
    worker_1 = Worker(
        env.client,
        task_queue=queue,
        workflows=[CaseInvestigationWorkflow],
        activities=ACTIVITIES,
        max_cached_workflows=0,
    )
    async with worker_1:
        while (
            await handle.query(CaseInvestigationWorkflow.get_case)
        ).status != Status.DISPOSITION_PROPOSED:
            await env.sleep(0.2)
    # worker #1 is gone; the Workflow is still open, waiting durably
    async with build_worker(env.client, queue):  # worker #2 replays history, then continues
        await handle.execute_update(
            CaseInvestigationWorkflow.decide, DecisionRequest("approve", "vihag")
        )
        case = await handle.result()
    assert case.status == Status.SUBMITTED
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        async for e in handle.fetch_history_events()
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert scheduled.count("llm_create") == 9 and scheduled.count("soar_submit") == 1
