"""The four SENTINEL agents. They run *inside* the Workflow, so they may only call Activities.

Base `Agent._converse` is the Temporal AI-cookbook loop in one place: ask the model (llm_create
Activity); if it wants a tool, run it (run_tool Activity) and ask again, until it answers. On the
last allowed turn no tools are offered, so the model must answer. Every turn is a recorded event:
a crash mid-loop resumes at the next turn, not from scratch. Subclasses only choose instructions,
tools, answer keys, and whether to loop.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, ClassVar

from temporalio.exceptions import ApplicationError

from soc.activities import call, llm_create, run_tool
from soc.models import (
    Alert,
    AskResult,
    Case,
    Disposition,
    Evidence,
    Finding,
    LLMRequest,
    ToolCall,
    Triage,
)


class Agent:
    name: ClassVar[str]
    instructions: ClassVar[str]
    tools: ClassVar[tuple[str, ...]] = ()
    loops: ClassVar[bool] = False  # False: cookbook "tool call" (<=1 tool); True: "agentic loop"
    answer_keys: ClassVar[list[str]] = ["findings"]

    def __init__(self, scenario: str, max_steps: int = 6) -> None:
        self.scenario = scenario
        self.max_tool_calls = max_steps if self.loops else 1

    async def _converse(self, prompt: str) -> tuple[dict[str, Any], list[Evidence]]:
        system = f"{self.instructions} Answer with a JSON object with keys: {self.answer_keys}."
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        evidence: list[Evidence] = []
        for step in range(self.max_tool_calls + 1):
            tools = list(self.tools) if step < self.max_tool_calls else []  # last turn: answer
            request = LLMRequest(self.name, self.scenario, step, messages, tools, self.answer_keys)
            reply = await call(llm_create, request)
            if reply.tool is None:
                assert reply.content is not None
                return reply.content, evidence
            result = await call(run_tool, ToolCall(self.scenario, reply.tool, reply.args))
            evidence.append(Evidence(result.get("ref", reply.tool), reply.tool, self.name, result))
            fn = {"name": reply.tool, "arguments": json.dumps(reply.args)}
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": reply.call_id, "type": "function", "function": fn}],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": reply.call_id, "content": json.dumps(result)}
            )
        raise ApplicationError(f"{self.name} hit its step cap without an answer")

    def _findings(self, answer: dict[str, Any]) -> list[Finding]:
        return [Finding(agent=self.name, **f) for f in answer.get("findings", [])]


class TriageAgent(Agent):
    name = "triage"
    instructions = "You triage SOC alerts. Return severity, verdict, confidence (0-1), rationale."
    tools = ("list_alert_evidence",)
    answer_keys = ["severity", "verdict", "confidence", "rationale"]

    async def run(self, alert: Alert) -> Triage:
        answer, _ = await self._converse(f"Triage this alert: {json.dumps(asdict(alert))}")
        return Triage(
            answer["severity"], answer["verdict"], float(answer["confidence"]), answer["rationale"]
        )


class EvidenceAgent(Agent):
    name = "evidence"
    instructions = "Gather evidence for the alert with your tools, then state cited findings."
    tools = ("search_evidence", "list_alert_evidence", "lookup_asset")
    loops = True

    async def run(self, alert: Alert) -> tuple[list[Evidence], list[Finding]]:
        answer, evidence = await self._converse(f"Investigate: {alert.title} on {alert.host}")
        return evidence, self._findings(answer)


class ThreatAgent(Agent):
    name = "threat"
    instructions = "Enrich indicators and map behaviour to MITRE ATT&CK, then state findings."
    tools = ("enrich_ioc", "lookup_mitre")
    loops = True

    async def run(self, alert: Alert) -> tuple[list[Evidence], list[Finding]]:
        answer, evidence = await self._converse(f"Threat-intel review: {alert.title}")
        return evidence, self._findings(answer)


class DispositionAgent(Agent):
    name = "disposition"
    instructions = "Propose one SOAR action for the case, citing finding indexes."
    answer_keys = ["action", "rationale", "cited_findings"]

    async def run(self, case: Case) -> Disposition:
        findings = [f"[{i}] {f.statement}" for i, f in enumerate(case.findings)]
        answer, _ = await self._converse("Findings:\n" + "\n".join(findings))
        return Disposition(answer["action"], answer["rationale"], answer["cited_findings"])


class InvestigatorAgent(Agent):
    """Answers an analyst's follow-up question (runs inside AskAgentWorkflow)."""

    name = "ask"
    instructions = "Answer the SOC analyst's question about this case using your tools."
    tools = ("search_logins", "lookup_exemptions", "search_evidence", "lookup_asset", "enrich_ioc")
    loops = True
    answer_keys = ["answer", "findings"]

    async def run(self, alert: Alert, question: str) -> AskResult:
        prompt = f"Case alert: {json.dumps(asdict(alert))}\nAnalyst question: {question}"
        answer, evidence = await self._converse(prompt)
        return AskResult(answer["answer"], evidence, self._findings(answer))
