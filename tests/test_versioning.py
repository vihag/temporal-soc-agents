"""Workflow versioning: histories recorded by the *previous* release must still replay on the
current code. The JSON files (file name = workflow id) were captured from main before the v3
changes, so a change that would break in-flight cases (a new command without
`workflow.patched`) fails here, in CI, before it reaches a worker."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.workflow import PAGE_BACKUP, AskAgentWorkflow, CaseInvestigationWorkflow

HISTORIES = sorted((Path(__file__).parent / "histories").glob("*.json"))


@pytest.mark.parametrize("path", HISTORIES, ids=[p.stem for p in HISTORIES])
async def test_previous_release_histories_still_replay(path: Path):
    history = WorkflowHistory.from_json(path.stem, path.read_text())
    replayer = Replayer(workflows=[CaseInvestigationWorkflow, AskAgentWorkflow])
    await replayer.replay_workflow(history)  # raises on any non-determinism


async def test_new_escalations_take_the_patched_branch(env, settings):
    """A case escalated on today's code records the patch marker and pages the backup."""
    sim = SIMULATIONS["approval_timeout_escalated"]
    case = await run_simulation(env.client, sim, settings, env.sleep, Narrator(lambda _: None))
    history = await env.client.get_workflow_handle(case.case_id).fetch_history()
    markers = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED]
    assert any(PAGE_BACKUP.encode() in e.SerializeToString() for e in markers)
    notifies = [
        e
        for e in history.events
        if e.activity_task_scheduled_event_attributes.activity_type.name == "notify"
    ]
    assert len(notifies) == 3  # awaiting approval, escalation, and the new backup page
    await Replayer(workflows=[CaseInvestigationWorkflow, AskAgentWorkflow]).replay_workflow(history)
