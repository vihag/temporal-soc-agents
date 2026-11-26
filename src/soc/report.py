"""The shift report: a Temporal Schedule starts a small Workflow every 8 hours.

A Schedule is Temporal's durable cron. The server owns the timetable, so no worker or cron
box has to be up at the right minute. Missed runs follow the catch-up policy, and overlap=SKIP
means a slow report never piles up behind itself. "Run now" (`trigger`) uses the same path.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

with workflow.unsafe.imports_passed_through():
    from soc.activities import call, notify, shift_report
    from soc.models import ShiftReport
    from soc.visibility import case_query

SCHEDULE_ID = "soc-shift-report"
EVERY = timedelta(hours=8)


@workflow.defn
class ShiftReportWorkflow:
    """Count cases by status and open load per analyst, then page the shift lead."""

    @workflow.run
    async def run(self) -> ShiftReport:
        report = await call(shift_report, case_query())
        await call(notify, report.summary())
        return report


async def ensure_schedule(client: Client, task_queue: str) -> None:
    """Create the schedule once (idempotent: an existing one is left alone)."""
    action = ScheduleActionStartWorkflow(
        ShiftReportWorkflow.run, id=SCHEDULE_ID, task_queue=task_queue
    )
    schedule = Schedule(
        action=action,
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=EVERY)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    with contextlib.suppress(ScheduleAlreadyRunningError):
        await client.create_schedule(SCHEDULE_ID, schedule)
