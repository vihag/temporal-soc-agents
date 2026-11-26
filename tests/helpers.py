"""Small helpers shared by the tests."""

from __future__ import annotations

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle

from soc.models import Case
from soc.simulate import Narrator, Simulation, run_simulation


async def play(env, settings, sim: Simulation, log: list[str] | None = None) -> Case:
    """Run one simulation on the time-skipping server; narration goes to `log` (or nowhere)."""
    narrator = Narrator(log.append if log is not None else lambda _: None)
    return await run_simulation(env.client, sim, settings, env.sleep, narrator)


async def attempts_of(handle: WorkflowHandle, activity: str) -> list[int]:
    """The attempt number of every start of `activity` in the Workflow's history."""
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
