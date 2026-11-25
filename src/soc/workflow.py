"""CaseInvestigationWorkflow: one durable Workflow per SOC alert. Read `run` top to bottom.

`run` is the happy path *and* the state machine: every `self._enter(Status.X, ...)` line is a
status change, and there is no other writer. Temporal makes the plain code durable. If the worker
dies at any line, a new worker replays history and continues from that line, without re-calling
finished Activities. No transitions table, no 409 guards, no polling jobs.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from soc.activities import call, fetch_alert, notify, soar_submit
    from soc.agents import DispositionAgent, EvidenceAgent, ThreatAgent, TriageAgent
    from soc.models import (
        ApprovalStatus,
        Case,
        CaseInput,
        DecisionRequest,
        RoutingPolicy,
        SoarSubmit,
        Status,
        TimelineEntry,
    )

MAX_REWORK = 2  # an analyst may reject (and send back for re-investigation) at most twice


@workflow.defn
class CaseInvestigationWorkflow:
    def __init__(self) -> None:
        self.case = Case()
        self.awaiting_decision = False
        self.decision: DecisionRequest | None = None

    @workflow.run
    async def run(self, inp: CaseInput) -> Case:
        case = self.case
        case.case_id = workflow.info().workflow_id
        case.alert = await call(fetch_alert, inp.scenario)
        self._enter(Status.NEW, f"Alert {case.alert.alert_id}: {case.alert.title}")

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
        """Human in the loop. A durable timer, not a sleeping thread: no worker needs to be up.

        After `approval_timeout_s` with no decision we escalate and keep waiting.
        """
        self.decision, self.awaiting_decision = None, True
        await call(notify, f"{self.case.case_id}: disposition awaiting approval")
        try:
            await workflow.wait_condition(
                lambda: self.decision is not None, timeout=timedelta(seconds=inp.approval_timeout_s)
            )
        except TimeoutError:
            self._enter(Status.ESCALATED, f"No decision in {inp.approval_timeout_s}s; paged lead")
            await call(notify, f"ESCALATION {self.case.case_id}: approval overdue")
            await workflow.wait_condition(lambda: self.decision is not None)
        self.awaiting_decision = False
        assert self.decision
        self.case.decisions.append(self.decision)
        return self.decision

    def _enter(self, status: Status, note: str) -> Case:
        """The one place status changes. Also appends the timeline the UI and CLI show."""
        self.case.status = status
        self.case.timeline.append(TimelineEntry(workflow.now().isoformat(), status, note))
        return self.case

    @workflow.query
    def get_case(self) -> Case:
        return self.case

    @workflow.query
    def approval_pending(self) -> ApprovalStatus:
        return ApprovalStatus(self.awaiting_decision, self.case.disposition)

    @workflow.update
    async def decide(self, request: DecisionRequest) -> Case:
        """Analyst approves or rejects. Returns the case once the Workflow has acted on it."""
        self.decision = request
        await workflow.wait_condition(lambda: not self.awaiting_decision)
        return self.case

    @decide.validator
    def _validate_decide(self, request: DecisionRequest) -> None:
        """Runs before the Update is accepted; a rejection is never written to history."""
        if request.actor_type != "analyst":
            raise ValueError("only an analyst can decide")
        if request.decision not in ("approve", "reject"):
            raise ValueError(f"unknown decision {request.decision!r}")
        if not self.awaiting_decision or self.decision is not None:
            raise ValueError(f"no disposition awaiting a decision (status {self.case.status})")
