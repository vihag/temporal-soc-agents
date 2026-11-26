"""Domain data for one SOC case. Plain dataclasses, no I/O, safe inside the Workflow sandbox.

Temporal's default data converter serialises these to JSON for Activity inputs,
Query results and Update payloads, so the same types travel end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Route = Literal["autonomous", "collaborative", "review"]

ROSTER = ("vihag", "aisha", "ben")  # analysts on shift (seeded; no auth in this demo)


class Status(StrEnum):
    """Where a case is. Written only by CaseInvestigationWorkflow.run (workflow.py)."""

    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    DISPOSITION_PROPOSED = "DISPOSITION_PROPOSED"
    ESCALATED = "ESCALATED"
    PARKED = "PARKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class CaseInput:
    """Workflow input. Runtime knobs ride here because Workflow code must not read env vars."""

    scenario: str
    enable_threat: bool = True
    approval_timeout_s: int = 120
    max_agent_steps: int = 6
    assignee: str = "vihag"
    alert_id: str = ""  # a queued alert's own ID; empty = the recorded scenario's


@dataclass
class Alert:
    alert_id: str
    title: str
    severity: str
    host: str
    user: str
    privileged_account: bool = False
    crown_jewel: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Triage:
    severity: str
    verdict: str
    confidence: float
    rationale: str


@dataclass
class Evidence:
    """One tool result an agent saw. `ref` is what findings cite."""

    ref: str
    source: str
    agent: str
    content: dict[str, Any]


@dataclass
class Finding:
    agent: str
    statement: str
    evidence_refs: list[str]
    confidence: float
    kind: str = "observation"


@dataclass
class Disposition:
    action: str
    rationale: str
    cited_findings: list[int]


@dataclass
class DecisionRequest:
    """Payload of the `decide` Update."""

    decision: Literal["approve", "reject"]
    analyst_id: str
    rationale: str = ""
    actor_type: str = "analyst"


@dataclass
class ReassignRequest:
    to_analyst: str
    by: str
    reason: str = ""


@dataclass
class ParkRequest:
    seconds: int
    analyst_id: str
    reason: str = ""


@dataclass
class AskRequest:
    question: str
    analyst_id: str


@dataclass
class Question:
    """One analyst follow-up and the ask-agent's answer (filled in when its child Workflow ends)."""

    question: str
    analyst_id: str
    workflow_id: str
    answer: str = ""


@dataclass
class AskInput:
    scenario: str
    case_id: str
    alert: Alert
    question: str
    max_agent_steps: int = 6


@dataclass
class AskResult:
    answer: str
    evidence: list[Evidence]
    findings: list[Finding]


@dataclass
class QueueMessage:
    """One alert on the intake queue. `receipt` is how it is acknowledged (deleted)."""

    alert_id: str
    scenario: str
    receipt: str = ""
    approval_timeout_s: int = 120


@dataclass
class IntakeState:
    """Carried across continue-as-new, so counters and round robin survive a fresh history."""

    next_analyst: int = 0
    started: int = 0
    duplicates: int = 0
    recent: list[str] = field(default_factory=list)
    polls_per_run: int = 100
    poll_interval_s: float = 2.0


@dataclass
class SoarReceipt:
    soar_id: str
    created: bool


@dataclass
class SoarSubmit:
    scenario: str
    case_id: str
    action: str
    approver: str


@dataclass
class TimelineEntry:
    at: str
    status: Status
    note: str


@dataclass
class ApprovalStatus:
    """Result of the `approval_pending` Query: is a human decision being awaited, and on what."""

    pending: bool
    disposition: Disposition | None = None


@dataclass
class Case:
    """Everything the Workflow knows about the case. Returned as-is by the `get_case` Query."""

    case_id: str = ""
    status: Status = Status.NEW
    assignee: str = ""
    parked_until: str = ""
    alert: Alert | None = None
    triage: Triage | None = None
    route: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    disposition: Disposition | None = None
    decisions: list[DecisionRequest] = field(default_factory=list)
    soar: SoarReceipt | None = None
    questions: list[Question] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)


@dataclass
class LLMRequest:
    """One model turn. `step` lets the fixture client replay a recorded conversation."""

    agent: str
    scenario: str
    step: int
    messages: list[dict[str, Any]]
    tools: list[str]
    answer_keys: list[str] = field(default_factory=list)  # a final answer must have these


@dataclass
class LLMReply:
    """Either a final structured answer (`content`) or one tool call (`tool` + `args`)."""

    model: str  # the model that actually answered
    content: dict[str, Any] | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    tokens: tuple[int, int] = (0, 0)  # prompt, completion


@dataclass
class ToolCall:
    scenario: str
    name: str
    args: dict[str, Any]


class RoutingPolicy:
    """Pure rule for who handles a triaged case. Deliberately small and readable."""

    AUTO_VERDICTS = ("benign", "false_positive")
    AUTO_SEVERITIES = ("low", "medium")

    def decide(self, alert: Alert, triage: Triage) -> Route:
        if triage.confidence < 0.5:
            return "review"  # the model is unsure: a human starts from scratch
        if alert.privileged_account or alert.crown_jewel:
            return "collaborative"  # high-blast-radius cases always get a human approval
        if (
            triage.verdict in self.AUTO_VERDICTS
            and triage.severity in self.AUTO_SEVERITIES
            and triage.confidence >= 0.8
        ):
            return "autonomous"
        return "collaborative"
