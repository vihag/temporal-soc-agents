"""CaseInvestigationWorkflow: one durable Workflow per SOC alert. Read `_lifecycle` top to bottom.

`_lifecycle` is the happy path *and* the state machine: every `self._enter(Status.X, ...)` line is
a status change, and there is no other writer. Temporal makes the plain code durable. If the worker
dies at any line, a new worker replays history and continues from that line, without re-calling
finished Activities. No transitions table, no 409 guards, no polling jobs.

Analysts act on a running case through Updates (each with a validator): decide, reassign,
park / unpark, and ask (which runs AskAgentWorkflow as a child).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from soc.activities import call, fetch_alert, notify, soar_submit
    from soc.agents import (
        DispositionAgent,
        EvidenceAgent,
        InvestigatorAgent,
        ThreatAgent,
        TriageAgent,
    )
    from soc.models import (
        ROSTER,
        ApprovalStatus,
        AskInput,
        AskRequest,
        AskResult,
        Case,
        CaseInput,
        DecisionRequest,
        ParkRequest,
        Question,
        ReassignRequest,
        RoutingPolicy,
        SoarSubmit,
        Status,
        TimelineEntry,
    )
    from soc.visibility import ASSIGNEE, CASE_STATUS, SEVERITY

MAX_REWORK = 2  # an analyst may reject (and send back for re-investigation) at most twice
CLOSED = (Status.SUBMITTED, Status.AUTO_RESOLVED, Status.NEEDS_REVIEW)


@workflow.defn
class CaseInvestigationWorkflow:
    def __init__(self) -> None:
        self.case = Case()
        self.inp = CaseInput("")
        self.awaiting_decision = False
        self.decision: DecisionRequest | None = None

    @workflow.run
    async def run(self, inp: CaseInput) -> Case:
        self.inp = inp
        await self._lifecycle(inp)
        # Never close the case under a running Update (e.g. an analyst question in flight).
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.case

    async def _lifecycle(self, inp: CaseInput) -> Case:
        case = self.case
        case.case_id = workflow.info().workflow_id
        case.alert = await call(fetch_alert, inp)
        if inp.indexed:  # display fields for the Visibility list (no Query needed)
            workflow.upsert_memo({"title": case.alert.title, "host": case.alert.host})
        self._enter(Status.NEW, f"Alert {case.alert.alert_id}: {case.alert.title}")
        case.assignee = inp.assignee
        self._enter(Status.ASSIGNED, f"Assigned to {inp.assignee}")

        try:
            case.triage = await TriageAgent(inp.scenario).run(case.alert)
        except (ActivityError, ApplicationError) as err:
            return self._enter(Status.NEEDS_REVIEW, f"Triage model call failed: {err.cause or err}")
        case.route = RoutingPolicy().decide(case.alert, case.triage)
        self._enter(
            Status.TRIAGED, f"{case.triage.verdict} ({case.triage.confidence:.2f}) -> {case.route}"
        )
        if case.route == "autonomous":
            return self._enter(Status.AUTO_RESOLVED, "Policy allowed auto-resolve; no human needed")
        if case.route == "review":
            return self._enter(Status.NEEDS_REVIEW, "Low confidence; handed to an analyst")

        for round_no in range(1, MAX_REWORK + 2):
            self._enter(Status.INVESTIGATING, f"Round {round_no}: evidence and threat agents")
            await self._investigate(inp)
            disposition = case.disposition = await DispositionAgent(inp.scenario).run(case)
            self._enter(Status.DISPOSITION_PROPOSED, f"Proposed: {disposition.action}")
            decision = await self._wait_for_analyst(inp)
            if decision.decision == "approve":
                break
            self._enter(Status.REJECTED, f"{decision.analyst_id}: {decision.rationale}")
            if round_no > MAX_REWORK:
                return self._enter(Status.NEEDS_REVIEW, "Rework cap reached; analyst takes over")

        self._enter(Status.APPROVED, f"Approved by {decision.analyst_id}")
        submit = SoarSubmit(inp.scenario, case.case_id, disposition.action, decision.analyst_id)
        case.soar = await call(soar_submit, submit)
        outcome = "filed" if case.soar.created else "already filed; the retry was harmless"
        return self._enter(Status.SUBMITTED, f"SOAR ticket {case.soar.soar_id} ({outcome})")

    async def _investigate(self, inp: CaseInput) -> None:
        """Evidence and Threat agents run in parallel: plain asyncio.gather, both durable."""
        assert self.case.alert
        agents = [EvidenceAgent(inp.scenario, inp.max_agent_steps).run(self.case.alert)]
        if inp.enable_threat:
            agents.append(ThreatAgent(inp.scenario, inp.max_agent_steps).run(self.case.alert))
        for evidence, findings in await asyncio.gather(*agents):
            self.case.evidence += evidence
            self.case.findings += findings

    async def _wait_for_analyst(self, inp: CaseInput) -> DecisionRequest:
        """Human in the loop: durable timers, not sleeping threads; no worker needs to be up.

        The approval SLA timer escalates once; a parked case waits on its park timer instead.
        """
        self.decision, self.awaiting_decision = None, True
        await call(notify, f"{self.case.case_id}: disposition awaiting {self.case.assignee}")
        waiting_as = Status.DISPOSITION_PROPOSED
        while self.decision is None:
            if self.case.parked_until:
                await self._stay_parked(resume_as=waiting_as)
                continue
            sla = (
                None
                if waiting_as == Status.ESCALATED
                else timedelta(seconds=inp.approval_timeout_s)
            )
            try:
                await workflow.wait_condition(
                    lambda: self.decision is not None or bool(self.case.parked_until),
                    timeout=sla,
                    timeout_summary="approval SLA",
                )
            except TimeoutError:
                waiting_as = Status.ESCALATED
                self._enter(
                    Status.ESCALATED, f"No decision in {inp.approval_timeout_s}s; paged lead"
                )
                await call(notify, f"ESCALATION {self.case.case_id}: approval overdue")
        self.awaiting_decision = False
        self.case.decisions.append(self.decision)
        return self.decision

    async def _stay_parked(self, resume_as: Status) -> None:
        """PARKED until the park timer fires, the analyst unparks, or decides anyway."""
        until = datetime.fromisoformat(self.case.parked_until)
        secs = int((until - workflow.now()).total_seconds())
        self._enter(Status.PARKED, f"Parked by {self.case.assignee} for {secs}s (decide any time)")
        try:
            await workflow.wait_condition(
                lambda: not self.case.parked_until or self.decision is not None,
                timeout=max(until - workflow.now(), timedelta(seconds=1)),
                timeout_summary="park timer",
            )
            note = "Unparked by the analyst"
        except TimeoutError:
            note = "Park timer fired; case is back in the queue"
        self.case.parked_until = ""
        if self.decision is None:
            self._enter(resume_as, note)

    def _enter(self, status: Status, note: str) -> Case:
        """The one place status changes. Also appends the timeline the UI and CLI show."""
        self.case.status = status
        self._index()
        return self._note(note)

    def _index(self) -> None:
        """Keep the search attributes the case queue filters on in step with the case."""
        if not self.inp.indexed:
            return
        updates = [CASE_STATUS.value_set(self.case.status), ASSIGNEE.value_set(self.case.assignee)]
        if self.case.alert:
            updates.append(SEVERITY.value_set(self.case.alert.severity))
        workflow.upsert_search_attributes(updates)

    def _note(self, note: str) -> Case:
        """A timeline entry without a status change (reassign, questions)."""
        self.case.timeline.append(TimelineEntry(workflow.now().isoformat(), self.case.status, note))
        return self.case

    # ---- Queries: read-only views for the UI and CLI ----

    @workflow.query
    def get_case(self) -> Case:
        return self.case

    @workflow.query
    def approval_pending(self) -> ApprovalStatus:
        return ApprovalStatus(self.awaiting_decision, self.case.disposition)

    # ---- Updates: analyst actions. Validators run first; a refusal never reaches history ----

    @workflow.update
    async def decide(self, request: DecisionRequest) -> Case:
        """Analyst approves or rejects. Returns the case once the Workflow has acted on it."""
        self.decision = request
        await workflow.wait_condition(lambda: not self.awaiting_decision)
        return self.case

    @decide.validator
    def _validate_decide(self, request: DecisionRequest) -> None:
        if request.actor_type != "analyst":
            raise ValueError("only an analyst can decide")
        if request.decision not in ("approve", "reject"):
            raise ValueError(f"unknown decision {request.decision!r}")
        if not self.awaiting_decision or self.decision is not None:
            raise ValueError(f"no disposition awaiting a decision (status {self.case.status})")
        if request.analyst_id != self.case.assignee:
            raise ValueError(f"only the assignee ({self.case.assignee}) can decide")

    @workflow.update
    def reassign(self, request: ReassignRequest) -> Case:
        """Hand the case to another analyst. Nothing restarts: same step, same timeline."""
        previous, self.case.assignee = self.case.assignee, request.to_analyst
        self._index()
        return self._note(
            f"Reassigned {previous} -> {request.to_analyst} by {request.by}: {request.reason}"
        )

    @reassign.validator
    def _validate_reassign(self, request: ReassignRequest) -> None:
        if request.to_analyst not in ROSTER:
            raise ValueError(f"{request.to_analyst!r} is not on shift ({', '.join(ROSTER)})")
        if request.to_analyst == self.case.assignee:
            raise ValueError(f"already assigned to {request.to_analyst}")
        if self.case.status in CLOSED:
            raise ValueError("case is closed")

    @workflow.update
    async def park(self, request: ParkRequest) -> Case:
        """Snooze the case until a time; the approval SLA does not run while parked."""
        until = workflow.now() + timedelta(seconds=request.seconds)
        self.case.parked_until = until.isoformat()
        await workflow.wait_condition(lambda: self.case.status == Status.PARKED)
        return self.case

    @park.validator
    def _validate_park(self, request: ParkRequest) -> None:
        if not self.awaiting_decision or self.case.parked_until:
            raise ValueError("only a case waiting on its analyst can be parked")
        if request.analyst_id != self.case.assignee:
            raise ValueError(f"only the assignee ({self.case.assignee}) can park")
        if not 10 <= request.seconds <= 7 * 24 * 3600:
            raise ValueError("park for between 10 seconds and 7 days")

    @workflow.update
    async def unpark(self, analyst_id: str) -> Case:
        self.case.parked_until = ""
        await workflow.wait_condition(lambda: self.case.status != Status.PARKED)
        return self.case

    @unpark.validator
    def _validate_unpark(self, analyst_id: str) -> None:
        if self.case.status != Status.PARKED:
            raise ValueError("case is not parked")
        if analyst_id != self.case.assignee:
            raise ValueError(f"only the assignee ({self.case.assignee}) can unpark")

    @workflow.update
    async def ask(self, request: AskRequest) -> Question:
        """Analyst follow-up: an agent loop runs as a child Workflow; findings join the case."""
        assert self.case.alert
        question = Question(
            request.question,
            request.analyst_id,
            f"{self.case.case_id}-ask-{len(self.case.questions) + 1}",
        )
        self.case.questions.append(question)
        self._note(f"{request.analyst_id} asked the agent: {request.question}")
        result: AskResult = await workflow.execute_child_workflow(
            AskAgentWorkflow.run,
            AskInput(
                self.inp.scenario,
                self.case.case_id,
                self.case.alert,
                request.question,
                self.inp.max_agent_steps,
            ),
            id=question.workflow_id,
        )
        question.answer = result.answer
        self.case.evidence += result.evidence
        self.case.findings += result.findings
        self._note(f"Agent answered: {result.answer}")
        return question

    @ask.validator
    def _validate_ask(self, request: AskRequest) -> None:
        if not request.question.strip():
            raise ValueError("ask a question")
        if request.analyst_id not in ROSTER:
            raise ValueError(f"{request.analyst_id!r} is not on shift")
        if self.case.alert is None or self.case.status in CLOSED:
            raise ValueError(f"cannot ask while the case is {self.case.status}")


@workflow.defn
class AskAgentWorkflow:
    """One analyst question, answered by an agent loop. A child Workflow so each question has
    its own history (and retries) in the Temporal UI, separate from the case."""

    @workflow.run
    async def run(self, inp: AskInput) -> AskResult:
        agent = InvestigatorAgent(inp.scenario, inp.max_agent_steps)
        return await agent.run(inp.alert, inp.question)
