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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.service import RPCError

from soc import activities, workflow
from soc.models import CaseInput, DecisionRequest
from soc.scenarios import Scenario
from soc.settings import Settings, connect
from soc.simulate import SIMULATIONS, Narrator, run_simulation
from soc.workflow import CaseInvestigationWorkflow

E = EventType
app = FastAPI(title="SOC case console")
_client: Client | None = None
_demo_tasks: dict[str, asyncio.Task[Any]] = {}


async def client() -> Client:
    global _client
    _client = _client or await connect()
    return _client


class StartBody(BaseModel):
    scenario: str = "hitl-confirm-01"


class DecideBody(BaseModel):
    decision: str
    rationale: str = ""
    analyst_id: str = "vihag"


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
    }


@app.get("/api/source/{module}", response_class=PlainTextResponse)
async def source(module: str) -> str:
    """The real code, so the UI can show (and highlight) exactly what is running."""
    return inspect.getsource({"workflow": workflow, "activities": activities}[module])


@app.get("/api/cases")
async def list_cases() -> list[dict[str, Any]]:
    c = await client()
    rows: list[dict[str, Any]] = []
    async for wf in c.list_workflows('WorkflowType="CaseInvestigationWorkflow"', limit=30):
        if any(r["id"] == wf.id for r in rows):
            continue  # newest run first; older runs of a re-used case ID live in the Temporal UI
        rows.append(
            {
                "id": wf.id,
                "run_status": wf.status.name if wf.status else "",
                "started": wf.start_time.isoformat(),
            }
        )
    cases = await asyncio.gather(*(_query(c, r["id"]) for r in rows), return_exceptions=True)
    for row, case in zip(rows, cases, strict=True):
        if isinstance(case, dict):
            alert = case["alert"] or {}
            row |= {
                "status": case["status"],
                "title": alert.get("title", ""),
                "severity": alert.get("severity", ""),
                "host": alert.get("host", ""),
            }
    return rows


async def _query(c: Client, wf_id: str) -> dict[str, Any]:
    return asdict(await c.get_workflow_handle(wf_id).query(CaseInvestigationWorkflow.get_case))


@app.post("/api/cases")
async def start_case(body: StartBody) -> dict[str, str]:
    settings, c = Settings.from_env(), await client()
    alert_id = Scenario.load(body.scenario, settings.fixtures_dir).alert["alert_id"]
    inp = CaseInput(
        body.scenario, settings.enable_threat, settings.approval_timeout_s, settings.max_agent_steps
    )
    handle = await c.start_workflow(
        CaseInvestigationWorkflow.run, inp, id=f"case-{alert_id}", task_queue=settings.task_queue
    )
    return {"id": handle.id}


@app.get("/api/cases/{wf_id}")
async def get_case(wf_id: str) -> dict[str, Any]:
    handle = (await client()).get_workflow_handle(wf_id)
    case = asdict(await handle.query(CaseInvestigationWorkflow.get_case))
    pending = await handle.query(CaseInvestigationWorkflow.approval_pending)
    return case | {"approval_pending": pending.pending}


@app.post("/api/cases/{wf_id}/decide")
async def decide(wf_id: str, body: DecideBody) -> dict[str, Any]:
    request = DecisionRequest(body.decision, body.analyst_id, body.rationale)  # type: ignore[arg-type]
    handle = (await client()).get_workflow_handle(wf_id)
    try:
        return asdict(await handle.execute_update(CaseInvestigationWorkflow.decide, request))
    except WorkflowUpdateFailedError as err:  # the validator said no; nothing was recorded
        raise HTTPException(409, str(err.cause or err)) from err
    except RPCError as err:  # e.g. the case already finished
        raise HTTPException(409, err.message) from err


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
                bars.append(
                    {
                        "kind": "workflow",
                        "label": "CaseInvestigationWorkflow",
                        "start": at,
                        "end": None,
                    }
                )
            elif t in (
                E.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                E.EVENT_TYPE_WORKFLOW_EXECUTION_TERMINATED,
                E.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
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
                by_id[e.event_id] = bar = {
                    "kind": "timer",
                    "label": f"approval timer ({secs}s)",
                    "start": at,
                    "end": None,
                }
                bars.append(bar)
            elif t == E.EVENT_TYPE_TIMER_FIRED:
                by_id[e.timer_fired_event_attributes.started_event_id] |= {"end": at, "fired": True}
            elif t == E.EVENT_TYPE_TIMER_CANCELED:
                by_id[e.timer_canceled_event_attributes.started_event_id]["end"] = at
            elif t == E.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
                arg = self._payload(
                    e.workflow_execution_update_accepted_event_attributes.accepted_request.input.args
                )
                bars.append(
                    {
                        "kind": "update",
                        "label": f"decide({arg.get('decision')})",
                        "start": at,
                        "end": at,
                        "input": arg,
                    }
                )
        return bars


def _label(name: str, arg: Any) -> str:
    if name == "llm_create":
        return f"llm_create · {arg['agent']} #{arg['step']}"
    if name == "run_tool":
        return f"run_tool · {arg['name']}"
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
        from soc.worker import build_worker

        os.environ.setdefault("FIXTURE_LATENCY_S", "0.7")  # watchable demo pace
        logging.getLogger("temporalio.activity").setLevel(logging.ERROR)  # demo faults are expected
        logging.getLogger("temporalio.worker").setLevel(logging.ERROR)
        settings = Settings.from_env()
        ensure_mocks(settings)
        async with build_worker(await client(), settings.task_queue):
            await server.serve()

    asyncio.run(main())
