"""Command line: run the worker, drive a case by hand, or run the self-driving simulations.

    python -m soc worker
    python -m soc api --with-worker   # UI on :8080 (+ worker and mocks in-process)
    python -m soc start --scenario hitl-confirm-01
    python -m soc view case-SPL-2026-0001
    python -m soc decide case-SPL-2026-0001 approve --analyst vihag --rationale "matches"
    python -m soc simulate --all
    python -m soc grok-smoke          # one live Grok call; exits 2 if XAI_API_KEY is unset

The CLI talks to Temporal directly (start, Query, Update); it never holds case state itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import urllib.request

from soc.models import Case, CaseInput, DecisionRequest
from soc.scenarios import Scenario
from soc.settings import Settings, connect
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.worker import build_worker, run_worker
from soc.workflow import CaseInvestigationWorkflow


def print_case(case: Case) -> None:
    print(f"{case.case_id}  [{case.status}]  route={case.route or '-'}")
    if case.alert:
        print(f"  alert: {case.alert.title} on {case.alert.host} ({case.alert.user})")
    Narrator().case(case)
    for i, f in enumerate(case.findings):
        print(f"  finding [{i}] ({f.agent}) {f.statement}  cites {', '.join(f.evidence_refs)}")
    if case.disposition:
        print(f"  disposition: {case.disposition.action} - {case.disposition.rationale}")
    if case.soar:
        print(f"  SOAR: {case.soar.soar_id} (created={case.soar.created})")


async def start(scenario: str) -> None:
    settings = Settings.from_env()
    alert_id = Scenario.load(scenario, settings.fixtures_dir).alert["alert_id"]
    inp = CaseInput(
        scenario, settings.enable_threat, settings.approval_timeout_s, settings.max_agent_steps
    )
    client = await connect()
    handle = await client.start_workflow(
        CaseInvestigationWorkflow.run, inp, id=f"case-{alert_id}", task_queue=settings.task_queue
    )
    print(f"Started {handle.id}. View: python -m soc view {handle.id}")


async def view(workflow_id: str) -> None:
    client = await connect()
    print_case(
        await client.get_workflow_handle(workflow_id).query(CaseInvestigationWorkflow.get_case)
    )


async def decide(workflow_id: str, decision: str, analyst: str, rationale: str) -> None:
    client = await connect()
    request = DecisionRequest(decision, analyst, rationale)  # type: ignore[arg-type]
    handle = client.get_workflow_handle(workflow_id)
    print_case(await handle.execute_update(CaseInvestigationWorkflow.decide, request))


def ensure_mocks(settings: Settings) -> None:
    """Start Mock Splunk / Mock SOAR in-process unless they are already running."""
    from services.common import serve_in_thread
    from services.mock_soar import soar_server
    from services.mock_splunk import splunk_server

    for url, factory in ((settings.splunk_url, splunk_server), (settings.soar_url, soar_server)):
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=1)
        except OSError:
            serve_in_thread(factory(int(url.rsplit(":", 1)[1])))


async def grok_smoke() -> int:
    """One live triage turn against Grok. Exit 2 without a key (never spends by accident)."""
    from soc.llm import GrokClient
    from soc.models import LLMRequest

    settings = Settings.from_env()
    if not settings.xai_api_key:
        print("XAI_API_KEY is not set; skipping live Grok smoke test.")
        return 2
    messages = [{"role": "user", "content": 'Reply with JSON {"ok": true}.'}]
    reply = await GrokClient(settings).complete(LLMRequest("smoke", "", 0, messages, []))
    print(f"model={reply.model} tokens={reply.tokens} content={reply.content}")
    return 0


async def simulate(names: list[str]) -> None:
    """Run scenarios end to end against the configured Temporal server, with an in-process
    worker and (if not already running) the mock services."""
    settings = Settings.from_env()
    logging.getLogger("temporalio.activity").setLevel(logging.ERROR)  # injected faults are expected
    logging.getLogger("temporalio.worker").setLevel(logging.ERROR)
    ensure_mocks(settings)
    client = await connect()
    async with build_worker(client, settings.task_queue):
        for name in names:
            sim = SIMULATIONS[name]
            print(f"\n=== {sim.name}: {sim.story}")
            case = await run_simulation(client, sim, settings, asyncio.sleep, Narrator())
            verdict = "PASS" if case.status == sim.expect else "FAIL"
            print(f"  {verdict}: ended {case.status} (expected {sim.expect})")
    print("\nBrowse every run, event by event, in the Temporal UI: http://localhost:8233")


def main() -> None:
    parser = argparse.ArgumentParser(prog="soc", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker")
    sub.add_parser("grok-smoke")
    sub.add_parser("api").add_argument("--with-worker", action="store_true")
    sub.add_parser("start").add_argument("--scenario", default="hitl-confirm-01")
    sub.add_parser("view").add_argument("workflow_id")
    d = sub.add_parser("decide")
    d.add_argument("workflow_id")
    d.add_argument("decision", choices=["approve", "reject"])
    d.add_argument("--analyst", default="vihag")
    d.add_argument("--rationale", default="")
    s = sub.add_parser("simulate")
    s.add_argument("names", nargs="*", help="any of: " + ", ".join(SIMULATIONS))
    s.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.cmd == "simulate" and (unknown := set(args.names) - set(SIMULATIONS)):
        parser.error(f"unknown simulation(s): {', '.join(sorted(unknown))}")

    match args.cmd:
        case "worker":
            asyncio.run(run_worker())
        case "start":
            asyncio.run(start(args.scenario))
        case "view":
            asyncio.run(view(args.workflow_id))
        case "decide":
            asyncio.run(decide(args.workflow_id, args.decision, args.analyst, args.rationale))
        case "api":
            from soc.api import serve

            serve(args.with_worker)
        case "grok-smoke":
            raise SystemExit(asyncio.run(grok_smoke()))
        case "simulate":
            asyncio.run(simulate(list(SIMULATIONS) if args.all or not args.names else args.names))
