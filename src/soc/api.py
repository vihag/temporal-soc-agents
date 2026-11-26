"""Analyst web API + UI. A stateless Temporal client: start, Query, Update, read history.

No case state lives here; refresh the page, restart this process, the Workflow still has it.
Run: python -m soc api [--with-worker]  ->  http://127.0.0.1:8080
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from google.protobuf import json_format
from pydantic import BaseModel
from temporalio.api.common.v1 import Payloads
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    WorkflowExecutionStatus,
    WorkflowUpdateFailedError,
    WorkflowUpdateStage,
)
from temporalio.service import RPCError

from soc import activities, intake, workflow
from soc.chaos import Chaos
from soc.codec import EncryptionCodec
from soc.history import HistoryBars
from soc.intake import INTAKE_ID, IntakeWorkflow
from soc.models import (
    ROSTER,
    AskRequest,
    DecisionRequest,
    ParkRequest,
    ReassignRequest,
)
from soc.report import EVERY, SCHEDULE_ID
from soc.scenarios import Scenario
from soc.settings import Settings, connect
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.upstream import request_json
from soc.visibility import ASSIGNEE, CASE_STATUS, SEVERITY, case_query
from soc.worker import WorkerSupervisor, bootstrap, quiet_expected_faults
from soc.workflow import CaseInvestigationWorkflow

app = FastAPI(title="SOC case console")
app.add_middleware(  # lets the Temporal UI (another origin) call /codec
    CORSMiddleware,
    allow_origins=os.environ.get("CODEC_CORS_ORIGINS", "http://localhost:8233").split(","),
    allow_methods=["POST"],
    allow_headers=["content-type", "x-namespace", "authorization"],
    allow_credentials=True,
)
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
    handle = await c.start_workflow(
        CaseInvestigationWorkflow.run,
        settings.case_input(body.scenario),
        id=f"case-{alert_id}",
        task_queue=settings.task_queue,
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
        queue = await request_json("GET", settings.queue_url, "/v1/depth", timeout=2)
    except (httpx.HTTPError, ValueError):
        queue = {"depth": None, "down": True}
    return {
        "id": INTAKE_ID,
        "stats": stats,
        "queue_depth": queue["depth"],
        "queue_down": queue["down"],
    }


@app.post("/codec/{op}")
async def codec_endpoint(op: str, request: Request) -> Response:
    """Codec server for the Temporal UI/CLI: it posts {"payloads": [...]} and gets the same shape
    back, encrypted or decrypted. The key never leaves this process."""
    codec = EncryptionCodec.from_env()
    if codec is None or op not in ("encode", "decode"):
        raise HTTPException(404, "set SOC_CODEC_KEY to enable the codec endpoint")
    payloads = json_format.Parse(await request.body(), Payloads())
    done = await (codec.encode if op == "encode" else codec.decode)(payloads.payloads)
    return Response(
        json_format.MessageToJson(Payloads(payloads=done)), media_type="application/json"
    )


@app.get("/api/report")
async def report() -> dict[str, Any]:
    """The shift-report Schedule: when it runs next, and what its latest run found."""
    c = await client()
    try:
        info = (await c.get_schedule_handle(SCHEDULE_ID).describe()).info
    except RPCError as err:
        raise HTTPException(404, err.message) from err
    latest = None
    for done in reversed(info.recent_actions):
        action = done.action
        if not isinstance(action, ScheduleActionExecutionStartWorkflow):
            continue
        run = c.get_workflow_handle(action.workflow_id, run_id=action.first_execution_run_id)
        if (await run.describe()).status == WorkflowExecutionStatus.COMPLETED:
            latest = await run.result()
            break
    return {
        "schedule_id": SCHEDULE_ID,
        "every_h": EVERY.total_seconds() / 3600,
        "runs": info.num_actions,
        "next": [t.isoformat() for t in info.next_action_times[:1]],
        "latest": latest,
    }


@app.post("/api/report")
async def run_report() -> dict[str, str]:
    """'Run now': trigger the Schedule once, exactly as if its time had come."""
    await (await client()).get_schedule_handle(SCHEDULE_ID).trigger()
    return {"message": f"triggered {SCHEDULE_ID}"}


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
    bars = await HistoryBars(c).build(events)
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

        os.environ.setdefault("FIXTURE_LATENCY_S", "0.7")  # watchable demo pace
        quiet_expected_faults()
        settings = Settings.from_env()
        ensure_mocks(settings)
        global _supervisor
        _supervisor = WorkerSupervisor(await client(), settings.task_queue)
        _supervisor.start()
        await bootstrap(await client(), settings)
        try:
            await server.serve()
        finally:
            await _supervisor.stop()

    asyncio.run(main())
