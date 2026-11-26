"""IntakeWorkflow: drains the alert queue and opens one case per alert. Runs forever.

Why a Workflow and not a cron script: the loop itself is durable. A queue outage is just an
Activity that keeps retrying (alerts wait in the queue, nothing is lost); a duplicate alert is a
child-Workflow start that Temporal refuses (case IDs are unique); and every N polls the loop
continues as new, so its history stays small while its counters carry over.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from soc.activities import ack_alerts, call, receive_alerts
    from soc.models import ROSTER, CaseInput, IntakeState, QueueMessage
    from soc.workflow import CaseInvestigationWorkflow

INTAKE_ID = "alert-intake"


@workflow.defn
class IntakeWorkflow:
    def __init__(self) -> None:
        self.state = IntakeState()

    @workflow.run
    async def run(self, state: IntakeState) -> None:
        self.state = state
        for _ in range(state.polls_per_run):
            batch = await call(receive_alerts, 10)
            for message in batch:
                await self._open_case(message)
            if batch:
                await call(ack_alerts, [m.receipt for m in batch])
            await workflow.sleep(timedelta(seconds=state.poll_interval_s), summary="poll interval")
        workflow.continue_as_new(self.state)  # fresh history, same counters

    async def _open_case(self, message: QueueMessage) -> None:
        """Start case-{alert_id} as a child that outlives this loop; a duplicate is a no-op."""
        assignee = ROSTER[self.state.next_analyst % len(ROSTER)]
        inp = CaseInput(
            message.scenario,
            assignee=assignee,
            alert_id=message.alert_id,
            approval_timeout_s=message.approval_timeout_s,
        )
        try:
            await workflow.start_child_workflow(
                CaseInvestigationWorkflow.run,
                inp,
                id=f"case-{message.alert_id}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            self.state.duplicates += 1
            self._log(f"duplicate {message.alert_id} ignored (case already exists)")
            return
        self.state.next_analyst += 1  # round robin over the analysts on shift
        self.state.started += 1
        self._log(f"opened case-{message.alert_id} -> {assignee}")

    def _log(self, line: str) -> None:
        self.state.recent = [
            f"{workflow.now().isoformat(timespec='seconds')} {line}",
            *self.state.recent,
        ][:20]

    @workflow.query
    def stats(self) -> IntakeState:
        return self.state


async def ensure_intake(client: Client, task_queue: str) -> None:
    """Start the intake loop unless it is already running (safe to call from every worker)."""
    await client.start_workflow(
        IntakeWorkflow.run,
        IntakeState(),
        id=INTAKE_ID,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
