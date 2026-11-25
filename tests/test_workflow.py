"""Edges the simulations do not cover: routing to review, a fatal model error, duplicate case
IDs, Update validation, and deterministic replay."""

from __future__ import annotations

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Replayer

from soc.models import CaseInput, DecisionRequest, Status
from soc.simulate import SIMULATIONS, Narrator, run_simulation
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
