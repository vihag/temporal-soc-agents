"""Command line: run the processes, drive a case by hand, seed alerts, break things, simulate.

    python -m soc api --with-worker   # console on :8080 (+ worker and mocks in-process)
    python -m soc worker
    python -m soc seed                # push 8 alerts onto the queue (one is a duplicate)
    python -m soc chaos splunk 20     # queue | splunk | grok | grok429 | soar, for N seconds
    python -m soc view case-SPL-2026-1001
    python -m soc decide case-SPL-2026-1001 approve --analyst vihag --rationale "matches"
    python -m soc reassign case-SPL-2026-1006 aisha --by vihag --reason "urgent leave"
    python -m soc park case-SPL-2026-1006 120 --analyst aisha
    python -m soc ask case-SPL-2026-1003 "exemption tickets this week for this user?"
    python -m soc simulate --all      # five narrated self-driving scenarios
    python -m soc demo run [--fast]   # play demo/runbook.yaml against the console (--fast: e2e)
    python -m soc grok-smoke          # one live Grok call; exits 2 if XAI_API_KEY is unset

The CLI talks to Temporal directly (start, Query, Update); it never holds case state itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import urllib.request
from typing import Any

from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from soc.chaos import Chaos
from soc.models import AskRequest, Case, CaseInput, DecisionRequest, ParkRequest, ReassignRequest
from soc.scenarios import Scenario
from soc.settings import Settings, connect
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.worker import build_worker, run_worker
from soc.workflow import CaseInvestigationWorkflow


def print_case(case: Case) -> None:
    print(f"{case.case_id}  [{case.status}]  assignee={case.assignee}  route={case.route or '-'}")
    if case.alert:
        print(f"  alert: {case.alert.title} on {case.alert.host} ({case.alert.user})")
    Narrator().case(case)
    for i, f in enumerate(case.findings):
        print(f"  finding [{i}] ({f.agent}) {f.statement}  cites {', '.join(f.evidence_refs)}")
    if case.disposition:
        print(f"  disposition: {case.disposition.action} - {case.disposition.rationale}")
    if case.soar:
        print(f"  SOAR: {case.soar.soar_id} (created={case.soar.created})")


def ensure_mocks(settings: Settings) -> None:
    """Start any mock service that is not already running, in-process."""
    from services.common import serve_in_thread
    from services.mock_grok import grok_server
    from services.mock_queue import queue_server
    from services.mock_soar import soar_server
    from services.mock_splunk import splunk_server

    mocks: list[tuple[str, Any]] = [
        (settings.splunk_url, splunk_server),
        (settings.soar_url, soar_server),
        (settings.mock_grok_url.removesuffix("/v1"), grok_server),
        (settings.queue_url, queue_server),
    ]
    for url, factory in mocks:
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=1)
        except OSError:
            serve_in_thread(factory(int(url.rsplit(":", 1)[1])))


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
    handle = (await connect()).get_workflow_handle(workflow_id)
    print_case(await handle.query(CaseInvestigationWorkflow.get_case))


async def update(workflow_id: str, name: str, arg: Any) -> None:
    """Send one analyst Update (decide / reassign / park / unpark / ask) and show the result."""
    handle = (await connect()).get_workflow_handle(workflow_id)
    try:
        result = await handle.execute_update(name, arg, result_type=Case if name != "ask" else None)
    except WorkflowUpdateFailedError as err:  # the validator said no; nothing was recorded
        raise SystemExit(f"Refused: {err.cause or err}") from err
    except RPCError as err:  # e.g. the case already finished
        raise SystemExit(f"Refused: {err.message}") from err
    if isinstance(result, Case):
        print_case(result)
    else:
        print(f"Agent answered: {result['answer']}")


async def grok_smoke() -> int:
    """One live call against Grok. Exit 2 without a key (never spends by accident)."""
    from dataclasses import replace

    from soc.llm import GrokClient
    from soc.models import LLMRequest

    settings = replace(Settings.from_env(), grok_client="http")
    if not settings.xai_api_key:
        print("XAI_API_KEY is not set; skipping live Grok smoke test.")
        return 2
    messages = [{"role": "user", "content": 'Reply with JSON {"ok": true}.'}]
    reply = await GrokClient(settings).complete(LLMRequest("smoke", "", 0, messages, []))
    print(f"model={reply.model} tokens={reply.tokens} content={reply.content}")
    return 0


async def simulate(names: list[str]) -> None:
    """Run scenarios end to end with an in-process worker and (if needed) the mock services."""
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


async def chaos(target: str, seconds: float) -> None:
    code = 429 if target == "grok429" else 503
    print(await Chaos(Settings.from_env()).outage(target.removesuffix("429"), seconds, code))


async def seed() -> None:
    count = await Chaos(Settings.from_env()).seed()  # SPL-2026-1001 ... 1007
    print(f"Pushed {count} alerts onto the queue (the last one duplicates the first).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="soc", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("worker", "grok-smoke", "seed"):
        sub.add_parser(name)
    sub.add_parser("api").add_argument("--with-worker", action="store_true")
    sub.add_parser("start").add_argument("--scenario", default="hitl-confirm-01")
    sub.add_parser("view").add_argument("workflow_id")
    d = sub.add_parser("decide")
    d.add_argument("workflow_id")
    d.add_argument("decision", choices=["approve", "reject"])
    d.add_argument("--analyst", default="vihag")
    d.add_argument("--rationale", default="")
    r = sub.add_parser("reassign")
    r.add_argument("workflow_id")
    r.add_argument("to")
    r.add_argument("--by", default="vihag")
    r.add_argument("--reason", default="")
    p = sub.add_parser("park")
    p.add_argument("workflow_id")
    p.add_argument("seconds", type=int)
    p.add_argument("--analyst", default="vihag")
    u = sub.add_parser("unpark")
    u.add_argument("workflow_id")
    u.add_argument("--analyst", default="vihag")
    a = sub.add_parser("ask")
    a.add_argument("workflow_id")
    a.add_argument("question")
    a.add_argument("--analyst", default="vihag")
    c = sub.add_parser("chaos")
    c.add_argument("target", choices=["queue", "splunk", "grok", "grok429", "soar"])
    c.add_argument("seconds", type=float)
    dr = sub.add_parser("demo")
    dr.add_argument("action", choices=["run"])
    dr.add_argument("--fast", action="store_true", help="no presenter pauses (the e2e test)")
    dr.add_argument("--runbook", default="demo/runbook.yaml")
    dr.add_argument("--api", default="http://127.0.0.1:8080")
    s = sub.add_parser("simulate")
    s.add_argument("names", nargs="*", help="any of: " + ", ".join(SIMULATIONS))
    s.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.cmd == "simulate" and (unknown := set(args.names) - set(SIMULATIONS)):
        parser.error(f"unknown simulation(s): {', '.join(sorted(unknown))}")

    match args.cmd:
        case "worker":
            asyncio.run(run_worker())
        case "api":
            from soc.api import serve

            serve(args.with_worker)
        case "grok-smoke":
            raise SystemExit(asyncio.run(grok_smoke()))
        case "seed":
            asyncio.run(seed())
        case "chaos":
            asyncio.run(chaos(args.target, args.seconds))
        case "start":
            asyncio.run(start(args.scenario))
        case "view":
            asyncio.run(view(args.workflow_id))
        case "decide":
            req = DecisionRequest(args.decision, args.analyst, args.rationale)  # type: ignore[arg-type]
            asyncio.run(update(args.workflow_id, "decide", req))
        case "reassign":
            req2 = ReassignRequest(args.to, args.by, args.reason)
            asyncio.run(update(args.workflow_id, "reassign", req2))
        case "park":
            asyncio.run(update(args.workflow_id, "park", ParkRequest(args.seconds, args.analyst)))
        case "unpark":
            asyncio.run(update(args.workflow_id, "unpark", args.analyst))
        case "ask":
            asyncio.run(update(args.workflow_id, "ask", AskRequest(args.question, args.analyst)))
        case "demo":
            from soc.director import Director

            director = Director(args.api, fast=args.fast)
            asyncio.run(director.run(Director.load(args.runbook)))
        case "simulate":
            asyncio.run(simulate(list(SIMULATIONS) if args.all or not args.names else args.names))
