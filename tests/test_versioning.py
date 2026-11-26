"""Workflow versioning: histories recorded by the *previous* release must still replay on the
current code. These JSON files were captured from main before the v3 changes, so a change that
would break in-flight cases (a new command without `workflow.patched`) fails here, in CI, before
it reaches a worker."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from soc.workflow import AskAgentWorkflow, CaseInvestigationWorkflow

HISTORIES = sorted((Path(__file__).parent / "histories").glob("*.json"))


@pytest.mark.parametrize("path", HISTORIES, ids=[p.stem for p in HISTORIES])
async def test_previous_release_histories_still_replay(path: Path):
    history = WorkflowHistory.from_json(path.stem, path.read_text())
    replayer = Replayer(workflows=[CaseInvestigationWorkflow, AskAgentWorkflow])
    await replayer.replay_workflow(history)  # raises on any non-determinism
