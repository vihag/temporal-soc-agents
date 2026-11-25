"""Self-driving demo scenarios. Each one starts a real Workflow, plays the analyst, and narrates
what happened: the case timeline (from the `get_case` Query) and what Temporal did (from the real
event history). The same scenarios back the pytest suite.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle

from soc.models import Case, CaseInput, DecisionRequest, Status
from soc.scenarios import Scenario
from soc.settings import Settings
from soc.workflow import CaseInvestigationWorkflow

WAITING = (Status.DISPOSITION_PROPOSED, Status.ESCALATED)
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Simulation:
    name: str
    scenario: str
    story: str
    analyst: list[str] = field(
        default_factory=list
    )  # "approve" | "reject" | "ignore" (let timer fire)
    expect: Status = Status.SUBMITTED
    approval_timeout_s: int = 120


SIMULATIONS = {
    s.name: s
    for s in [
        Simulation(
            "auto_resolve",
            "auto-fp-01",
            "Benign admin script: policy auto-resolves, no human.",
            expect=Status.AUTO_RESOLVED,
        ),
        Simulation(
            "approve_to_submitted",
            "hitl-confirm-01",
            "Credential dumping on a finance host: agents investigate, analyst approves.",
            ["approve"],
        ),
        Simulation(
            "reject_then_reinvestigate",
            "hitl-reject-01",
            "Analyst rejects the first proposal; agents re-investigate; second one approved.",
            ["reject", "approve"],
        ),
        Simulation(
            "approval_timeout_escalated",
            "hitl-timeout-01",
            "Nobody answers: a durable timer fires, the case escalates, a late approval lands.",
            ["ignore", "approve"],
            approval_timeout_s=3,
        ),
        Simulation(
            "activity_failure_retry",
            "hitl-flaky-01",
            "Splunk times out, the model garbles a reply, SOAR's response is lost: retries heal.",
            ["approve"],
        ),
    ]
}


class Narrator:
    """Prints new timeline entries as the case moves, then a summary of the event history."""

    def __init__(self, out: Callable[[str], None] = print) -> None:
        self.out, self.seen = out, 0

    def case(self, case: Case) -> None:
        for entry in case.timeline[self.seen :]:
            local = datetime.fromisoformat(entry.at).astimezone().strftime("%H:%M:%S")
            self.out(f"  {local}  {entry.status:<20} {entry.note}")
        self.seen = len(case.timeline)

    async def temporal(self, handle: WorkflowHandle) -> None:
        names: dict[int, str] = {}
        ran: Counter[str] = Counter()
        lines, total = [], 0
        async for e in handle.fetch_history_events():
            total += 1
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                names[e.event_id] = e.activity_task_scheduled_event_attributes.activity_type.name
                ran[names[e.event_id]] += 1
            elif e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
                a = e.activity_task_started_event_attributes
                if a.attempt > 1:
                    name = names[a.scheduled_event_id]
                    lines.append(
                        f"retried {name}: succeeded on attempt {a.attempt} "
                        f"(last error: {a.last_failure.message})"
                    )
            elif e.event_type == EventType.EVENT_TYPE_TIMER_FIRED:
                lines.append("durable approval timer fired (no worker thread was held)")
            elif e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
                lines.append("Update `decide` passed its validator and was recorded")
        self.out(f"  What Temporal did ({total} history events, workflow {handle.id}):")
        self.out("    activities: " + ", ".join(f"{n} x{c}" for n, c in ran.items()))
        for line in lines:
            self.out(f"    {line}")


async def run_simulation(
    client: Client, sim: Simulation, settings: Settings, sleep: Sleep, narrator: Narrator
) -> Case:
    """Start the case, play the analyst's script, return the final case."""
    alert_id = Scenario.load(sim.scenario, settings.fixtures_dir).alert["alert_id"]
    inp = CaseInput(
        sim.scenario, settings.enable_threat, sim.approval_timeout_s, settings.max_agent_steps
    )
    handle = await client.start_workflow(
        CaseInvestigationWorkflow.run, inp, id=f"case-{alert_id}", task_queue=settings.task_queue
    )

    async def wait_until(done: Callable[[Case], bool]) -> None:
        while not done(case := await handle.query(CaseInvestigationWorkflow.get_case)):
            narrator.case(case)
            await sleep(0.2)
        narrator.case(case)

    for action in sim.analyst:
        await wait_until(lambda c: c.status in WAITING)
        if action == "ignore":
            await sleep(sim.approval_timeout_s + 1)
            await wait_until(lambda c: c.status == Status.ESCALATED)
            continue
        narrator.out(f"  {'':8}  analyst -> decide({action})")
        await handle.execute_update(
            CaseInvestigationWorkflow.decide,
            DecisionRequest(action, "vihag", f"simulated {action}"),  # type: ignore[arg-type]
        )
    case = await handle.result()
    narrator.case(case)
    await narrator.temporal(handle)
    return case
