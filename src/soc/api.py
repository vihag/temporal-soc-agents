"""Analyst web API + UI. A stateless Temporal client: start, Query, Update, read history.

No case state lives here; refresh the page, restart this process, the Workflow still has it.
Run: python -m soc api [--with-worker]  ->  http://127.0.0.1:8080
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.service import RPCError

from soc import activities, intake, workflow
from soc.chaos import Chaos
from soc.intake import INTAKE_ID, IntakeWorkflow
from soc.models import (
    ROSTER,
    AskRequest,
    CaseInput,
    DecisionRequest,
    ParkRequest,
    ReassignRequest,
)
from soc.scenarios import Scenario
from soc.settings import Settings, connect
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.visibility import ASSIGNEE, CASE_STATUS, SEVERITY, case_query, ensure_search_attributes
from soc.worker import WorkerSupervisor
from soc.workflow import CaseInvestigationWorkflow

E = EventType
app = FastAPI(title="SOC case console")
_client: Client | None = None
_supervisor: WorkerSupervisor | None = None  # set when the worker runs in this process
_demo_tasks: dict[str, asyncio.Task[Any]] = {}
_chaos_tasks: set[asyncio.Task[Any]] = set()
_caption: dict[str, Any] = {}  # the demo director's current caption (display only)


async def client() -> Client:
    global _client
    _client = _client or await connect()
    return _client


class StartBody(BaseModel):
    scenario: str = "hitl-confirm-01"


class ActionBody(BaseModel):
    """One body for every analyst action; each action reads the fields it needs."""

    analyst_id: str = "vihag"
    decision: str = ""
    rationale: str = ""
    to_analyst: str = ""
    reason: str = ""
    seconds: int = 0
    question: str = ""


class SeedBody(BaseModel):
    prefix: str = "SPL-2026"


class AlertBody(BaseModel):
    alert_id: str
    scenario: str = "hitl-confirm-01"
    approval_timeout_s: int = 120


class CaptionBody(BaseModel):
    text: str = ""
    step: int = 0
    total: int = 0


class ChaosBody(BaseModel):
    target: str  # queue | splunk | grok | soar | worker | sla
    seconds: float = 20
    code: int = 503


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "ui" / "index.html")


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    c = await client()
    ui = os.environ.get("TEMPORAL_UI_URL", "http://localhost:8233")
    return {
        "temporal_ui": f"{ui}/namespaces/{c.namespace}/workflows",
        "simulations": [{"name": s.name, "story": s.story} for s in SIMULATIONS.values()],
        "roster": list(ROSTER),
        "worker_in_process": _supervisor is not None,
    }


@app.get("/api/source/{module}", response_class=PlainTextResponse)
async def source(module: str) -> str:
    """The real code, so the UI can show (and highlight) exactly what is running."""
    modules = {"workflow": workflow, "activities": activities, "intake": intake}
    return inspect.getsource(modules[module])


@app.get("/api/cases")
async def list_cases(status: str = "", assignee: str = "") -> dict[str, Any]:
    """The case queue: one Visibility list query on the case search attributes."""
    c = await client()
    if not Settings.from_env().search_attributes:  # fallback: Query every recent case
        return {"query": "(search attributes off: one Query per case)", "rows": await _queried(c)}
    query, rows = case_query(status, assignee), list[dict[str, Any]]()
    async for wf in c.list_workflows(query, limit=100):
        if any(r["id"] == wf.id for r in rows):
            continue  # newest run first; older runs of a re-used case ID live in the Temporal UI
        sa, memo = wf.typed_search_attributes, await wf.memo()
        rows.append(
            {
                "id": wf.id,
                "alert_id": wf.id.removeprefix("case-"),
                "run_status": wf.status.name if wf.status else "",
                "started": wf.start_time.isoformat(),
                "status": sa.get(CASE_STATUS) or "",
                "assignee": sa.get(ASSIGNEE) or "",
                "severity": sa.get(SEVERITY) or "",
                "title": memo.get("title", ""),
                "host": memo.get("host", ""),
            }
        )
    return {"query": query, "rows": rows}


async def _queried(c: Client) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async for wf in c.list_workflows('WorkflowType="CaseInvestigationWorkflow"', limit=60):
        if not any(r["id"] == wf.id for r in rows):
            rows.append({"id": wf.id, "run_status": wf.status.name if wf.status else ""})
    cases = await asyncio.gather(*(_query(c, r["id"]) for r in rows), return_exceptions=True)
    for row, case in zip(rows, cases, strict=True):
        if isinstance(case, dict):
            alert = case["alert"] or {}
            row |= {
                "status": case["status"],
                "assignee": case["assignee"],
                "alert_id": alert.get("alert_id", ""),
                "title": alert.get("title", ""),
                "severity": alert.get("severity", ""),
                "host": alert.get("host", ""),
                "started": case["timeline"][0]["at"] if case["timeline"] else "",
            }
    return rows


async def _query(c: Client, wf_id: str) -> dict[str, Any]:
    return asdict(await c.get_workflow_handle(wf_id).query(CaseInvestigationWorkflow.get_case))


@app.post("/api/cases")
async def start_case(body: StartBody) -> dict[str, str]:
    settings, c = Settings.from_env(), await client()
    alert_id = Scenario.load(body.scenario, settings.fixtures_dir).alert["alert_id"]
    inp = CaseInput(
        body.scenario,
        settings.enable_threat,
        settings.approval_timeout_s,
        settings.max_agent_steps,
        indexed=settings.search_attributes,
    )
    handle = await c.start_workflow(
        CaseInvestigationWorkflow.run, inp, id=f"case-{alert_id}", task_queue=settings.task_queue
    )
    return {"id": handle.id}


@app.get("/api/cases/{wf_id}")
async def get_case(wf_id: str) -> dict[str, Any]:
    handle = (await client()).get_workflow_handle(wf_id)
    try:
        case = asdict(await handle.query(CaseInvestigationWorkflow.get_case))
    except RPCError as err:  # not opened yet (or no such case)
        raise HTTPException(404, err.message) from err
    pending = await handle.query(CaseInvestigationWorkflow.approval_pending)
    return case | {"approval_pending": pending.pending}


@app.post("/api/cases/{wf_id}/{action}")
async def act(wf_id: str, action: str, body: ActionBody) -> dict[str, Any]:
    """decide | reassign | park | unpark | ask, each a validated Workflow Update.

    `ask` returns as soon as the Workflow accepts it: the agent runs as a child Workflow and its
    answer lands on the case timeline (the UI keeps polling)."""
    handle = (await client()).get_workflow_handle(wf_id)
    wf = CaseInvestigationWorkflow
    try:
        match action:
            case "decide":
                req = DecisionRequest(body.decision, body.analyst_id, body.rationale)  # type: ignore[arg-type]
                return asdict(await handle.execute_update(wf.decide, req))
            case "reassign":
                req2 = ReassignRequest(body.to_analyst, body.analyst_id, body.reason)
                return asdict(await handle.execute_update(wf.reassign, req2))
            case "park":
                req3 = ParkRequest(body.seconds, body.analyst_id, body.reason)
                return asdict(await handle.execute_update(wf.park, req3))
            case "unpark":
                return asdict(await handle.execute_update(wf.unpark, body.analyst_id))
            case "ask":
                await handle.start_update(
                    wf.ask,
                    AskRequest(body.question, body.analyst_id),
                    wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                )
                return {"accepted": True}
    except WorkflowUpdateFailedError as err:  # the validator said no; nothing was recorded
        raise HTTPException(409, str(err.cause or err)) from err
    except RPCError as err:  # e.g. the case already finished
        raise HTTPException(409, err.message) from err
    raise HTTPException(404, f"unknown action {action}")


@app.get("/api/intake")
async def intake_stats() -> dict[str, Any]:
    """The intake loop's counters (a Query) plus the queue depth, for the intake panel."""
    c, settings = await client(), Settings.from_env()
    try:
        stats = asdict(await c.get_workflow_handle(INTAKE_ID).query(IntakeWorkflow.stats))
    except RPCError:
        stats = {}
    try:
        async with httpx.AsyncClient(timeout=2) as http:
            queue = (await http.get(f"{settings.queue_url}/v1/depth")).json()
    except (httpx.HTTPError, ValueError):
        queue = {"depth": None, "down": True}
    return {
        "id": INTAKE_ID,
        "stats": stats,
        "queue_depth": queue["depth"],
        "queue_down": queue["down"],
    }


@app.post("/api/seed")
async def seed(body: SeedBody | None = None) -> dict[str, int]:
    return {"pushed": await Chaos(Settings.from_env()).seed((body or SeedBody()).prefix)}


@app.post("/api/alerts")
async def push_alert(body: AlertBody) -> dict[str, str]:
    """Put one alert on the queue (what a SIEM would do). Intake opens case-{alert_id}."""
    await Chaos(Settings.from_env()).push(body.alert_id, body.scenario, body.approval_timeout_s)
    return {"case_id": f"case-{body.alert_id}"}


@app.post("/api/caption")
async def set_caption(body: CaptionBody) -> dict[str, Any]:
    """The demo director narrates here; the console shows it as a banner."""
    _caption.update(body.model_dump(), at=datetime.now().astimezone().isoformat())
    return _caption


@app.get("/api/caption")
async def get_caption() -> dict[str, Any]:
    return _caption


@app.post("/api/chaos")
async def chaos(body: ChaosBody) -> dict[str, str]:
    """Break something on purpose; Temporal's handling shows up on the case timelines."""
    if body.target == "worker":
        if _supervisor is None:
            raise HTTPException(409, "worker runs in another process: docker compose kill worker")
        task = asyncio.create_task(_supervisor.down_for(body.seconds))
        _chaos_tasks.add(task)
        task.add_done_callback(_chaos_tasks.discard)
        return {"message": f"worker stopped for {body.seconds:g}s"}
    tools = Chaos(Settings.from_env())
    if body.target == "sla":
        alert_id = f"SPL-2026-SLA-{datetime.now():%H%M%S}"
        await tools.sla_breach(alert_id)
        return {"message": f"queued {alert_id} with a 20s approval SLA"}
    return {"message": await tools.outage(body.target, body.seconds, body.code)}


@app.get("/api/cases/{wf_id}/history")
async def history(wf_id: str) -> dict[str, Any]:
    """Workflow event history, shaped as Gantt bars for the event timeline."""
    c = await client()
    handle = c.get_workflow_handle(wf_id)
    events = [e async for e in handle.fetch_history_events()]
    bars = HistoryBars(c).build(events)
    desc = await handle.describe()
    by_activity_id = {b.get("activity_id"): b for b in bars}
    for p in desc.raw_description.pending_activities:  # a retry in progress is not in history yet
        if p.attempt > 1 and (bar := by_activity_id.get(p.activity_id)):
            bar |= {"retrying": True, "attempt": p.attempt, "error": p.last_failure.message}
    return {
        "bars": bars,
        "running": desc.status is not None and desc.status.name == "RUNNING",
        "now": datetime.now().astimezone().isoformat(),
    }


def _ts(ts: Any) -> str:
    return ts.ToDatetime().astimezone().isoformat()


class HistoryBars:
    """Turns raw history events into bars: the workflow, each Activity (`failed_until` marks where
    failed attempts end and the successful one starts), timers, and Update markers. Payloads are
    decoded so the drawers can show real inputs and outputs."""

    def __init__(self, c: Client) -> None:
        self.decode = c.data_converter.payload_converter.from_payloads

    def _payload(self, payloads: Any) -> Any:
        values = self.decode(payloads.payloads) if payloads.payloads else []
        return values[0] if len(values) == 1 else values

    def build(self, events: list[Any]) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        by_id: dict[int, dict[str, Any]] = {}
        for e in events:
            t, at = e.event_type, _ts(e.event_time)
            if t == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                name = e.workflow_execution_started_event_attributes.workflow_type.name
                bars.append({"kind": "workflow", "label": name, "start": at, "end": None})
            elif t in (
                E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
                E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
                E.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW,
            ):
                bars[0]["end"] = at
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                a = e.activity_task_scheduled_event_attributes
                arg = self._payload(a.input)
                by_id[e.event_id] = bar = {
                    "kind": "activity",
                    "name": a.activity_type.name,
                    "label": _label(a.activity_type.name, arg),
                    "activity_id": a.activity_id,
                    "start": at,
                    "end": None,
                    "input": arg,
                    "attempt": 1,
                }
                bars.append(bar)
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
                a = e.activity_task_started_event_attributes
                if a.attempt > 1:
                    by_id[a.scheduled_event_id] |= {
                        "failed_until": at,
                        "attempt": a.attempt,
                        "error": a.last_failure.message,
                    }
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                a = e.activity_task_completed_event_attributes
                by_id[a.scheduled_event_id] |= {"end": at, "result": self._payload(a.result)}
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                a = e.activity_task_failed_event_attributes
                by_id[a.scheduled_event_id] |= {
                    "end": at,
                    "kind": "failed",
                    "error": a.failure.message,
                }
            elif t == E.EVENT_TYPE_TIMER_STARTED:
                a = e.timer_started_event_attributes
                secs = a.start_to_fire_timeout.ToSeconds()
                meta = e.user_metadata.summary if e.HasField("user_metadata") else None
                summary = self.decode([meta])[0] if meta and meta.data else ""
                by_id[e.event_id] = bar = {
                    "kind": "timer",
                    "label": f"{summary or 'timer'} ({_dur(secs)})",
                    "start": at,
                    "end": None,
                }
                bars.append(bar)
            elif t == E.EVENT_TYPE_TIMER_FIRED:
                by_id[e.timer_fired_event_attributes.started_event_id] |= {"end": at, "fired": True}
            elif t == E.EVENT_TYPE_TIMER_CANCELED:
                by_id[e.timer_canceled_event_attributes.started_event_id]["end"] = at
            elif t == E.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
                request = e.workflow_execution_update_accepted_event_attributes.accepted_request
                arg = self._payload(request.input.args)
                label = _update_label(request.input.name, arg)
                bars.append(
                    {"kind": "update", "label": label, "start": at, "end": at, "input": arg}
                )
            elif t == E.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
                a = e.start_child_workflow_execution_initiated_event_attributes
                by_id[e.event_id] = bar = {
                    "kind": "child",
                    "label": f"↳ child {a.workflow_id}",
                    "child_id": a.workflow_id,
                    "start": at,
                    "end": None,
                    "input": self._payload(a.input),
                }
                bars.append(bar)
            elif t == E.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
                a = e.child_workflow_execution_started_event_attributes
                if a.workflow_type.name == "CaseInvestigationWorkflow":  # abandoned: runs on alone
                    by_id[a.initiated_event_id]["end"] = at
            elif t == E.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED:
                a = e.child_workflow_execution_completed_event_attributes
                by_id[a.initiated_event_id] |= {"end": at, "result": self._payload(a.result)}
            elif t == E.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_FAILED:
                a = e.start_child_workflow_execution_failed_event_attributes
                by_id[a.initiated_event_id] |= {
                    "end": at,
                    "kind": "duplicate",
                    "label": f"⊘ duplicate {by_id[a.initiated_event_id]['child_id']} refused",
                    "error": "already exists: duplicate alert ignored",
                }
        return bars


def _dur(secs: int) -> str:
    return f"{secs // 60}m" if secs >= 120 else f"{secs}s"


def _update_label(name: str, arg: Any) -> str:
    detail = {
        "decide": lambda: arg.get("decision"),
        "reassign": lambda: f"→ {arg.get('to_analyst')}",
        "park": lambda: _dur(int(arg.get("seconds", 0))),
        "ask": lambda: "agent",
        "unpark": lambda: arg,
    }.get(name, lambda: "")()
    return f"{name}({detail})"


def _label(name: str, arg: Any) -> str:
    if name == "llm_create":
        return f"llm_create · {arg['agent']} #{arg['step']}"
    if name == "run_tool":
        return f"run_tool · {arg['name']}"
    if name == "fetch_alert":
        return f"fetch_alert · {arg.get('alert_id') or arg.get('scenario')}"
    return name


@app.post("/api/demo/{name}")
async def start_demo(name: str) -> dict[str, str]:
    """Self-demo: run a named simulation in the background; the UI watches its real history."""
    sim, settings, c = SIMULATIONS[name], Settings.from_env(), await client()
    if (old := _demo_tasks.get(name)) and not old.done():
        old.cancel()
    alert_id = Scenario.load(sim.scenario, settings.fixtures_dir).alert["alert_id"]
    _demo_tasks[name] = asyncio.create_task(
        run_simulation(c, sim, settings, _demo_sleep, Narrator(lambda _: None), restart=True)
    )
    return {"id": f"case-{alert_id}"}


async def _demo_sleep(seconds: float) -> None:
    await asyncio.sleep(2.5 if seconds == 0 else max(seconds, 1.0))  # watchable pace


def serve(with_worker: bool) -> None:
    """Run the API (and optionally a worker + mock services in the same process)."""
    import uvicorn

    async def main() -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=os.environ.get("API_HOST", "127.0.0.1"),
                port=int(os.environ.get("API_PORT", "8080")),
            )
        )
        if not with_worker:
            return await server.serve()
        from soc.cli import ensure_mocks
        from soc.intake import ensure_intake

        os.environ.setdefault("FIXTURE_LATENCY_S", "0.7")  # watchable demo pace
        logging.getLogger("temporalio.activity").setLevel(logging.ERROR)  # demo faults are expected
        logging.getLogger("temporalio.worker").setLevel(logging.ERROR)
        settings = Settings.from_env()
        ensure_mocks(settings)
        global _supervisor
        _supervisor = WorkerSupervisor(await client(), settings.task_queue)
        _supervisor.start()
        if settings.search_attributes:
            await ensure_search_attributes(await client())
        await ensure_intake(await client(), settings.task_queue, settings.search_attributes)
        try:
            await server.serve()
        finally:
            await _supervisor.stop()

    asyncio.run(main())
