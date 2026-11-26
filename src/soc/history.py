"""Event history -> Gantt bars for the console's event timeline and drawers.

`HistoryBars` turns raw Temporal history events into one bar per Workflow, Activity, timer,
Update and child Workflow. Payloads are decoded (through the client's data converter, so the
encryption codec applies) so the drawers can show real inputs and outputs.
"""

from __future__ import annotations

from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

E = EventType


def _ts(ts: Any) -> str:
    return ts.ToDatetime().astimezone().isoformat()


class HistoryBars:
    """Turns raw history events into bars: the workflow, each Activity (`failed_until` marks where
    failed attempts end and the successful one starts), timers, and Update markers. Payloads are
    decoded so the drawers can show real inputs and outputs."""

    def __init__(self, c: Client) -> None:
        self.converter = c.data_converter  # includes the encryption codec, when one is set

    async def _payload(self, payloads: Any) -> Any:
        values = await self.converter.decode(payloads.payloads) if payloads.payloads else []
        return values[0] if len(values) == 1 else values

    async def build(self, events: list[Any]) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        by_id: dict[int, dict[str, Any]] = {}  # event id -> the bar that event opened

        def open_bar(event_id: int, kind: str, label: str, at: str, **extra: Any) -> None:
            by_id[event_id] = bar = {
                "kind": kind,
                "label": label,
                "start": at,
                "end": None,
                **extra,
            }
            bars.append(bar)

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
                arg = await self._payload(a.input)
                label = _label(a.activity_type.name, arg)
                open_bar(e.event_id, "activity", label, at, name=a.activity_type.name)
                by_id[e.event_id] |= {"activity_id": a.activity_id, "input": arg, "attempt": 1}
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
                a = e.activity_task_started_event_attributes
                if a.attempt > 1:  # the earlier attempts failed; this one starts now
                    by_id[a.scheduled_event_id] |= {
                        "failed_until": at,
                        "attempt": a.attempt,
                        "error": a.last_failure.message,
                    }
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
                a = e.activity_task_completed_event_attributes
                by_id[a.scheduled_event_id] |= {"end": at, "result": await self._payload(a.result)}
            elif t == E.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                a = e.activity_task_failed_event_attributes
                by_id[a.scheduled_event_id] |= {
                    "end": at,
                    "kind": "failed",
                    "error": a.failure.message,
                }
            elif t == E.EVENT_TYPE_TIMER_STARTED:
                secs = e.timer_started_event_attributes.start_to_fire_timeout.ToSeconds()
                meta = e.user_metadata.summary if e.HasField("user_metadata") else None
                summary = (await self.converter.decode([meta]))[0] if meta and meta.data else ""
                open_bar(e.event_id, "timer", f"{summary or 'timer'} ({_dur(secs)})", at)
            elif t == E.EVENT_TYPE_TIMER_FIRED:
                by_id[e.timer_fired_event_attributes.started_event_id] |= {"end": at, "fired": True}
            elif t == E.EVENT_TYPE_TIMER_CANCELED:
                by_id[e.timer_canceled_event_attributes.started_event_id]["end"] = at
            elif t == E.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
                request = e.workflow_execution_update_accepted_event_attributes.accepted_request
                arg = await self._payload(request.input.args)
                label = _update_label(request.input.name, arg)
                bars.append(
                    {"kind": "update", "label": label, "start": at, "end": at, "input": arg}
                )
            elif t == E.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
                a = e.start_child_workflow_execution_initiated_event_attributes
                label, arg = f"↳ child {a.workflow_id}", await self._payload(a.input)
                open_bar(e.event_id, "child", label, at, child_id=a.workflow_id, input=arg)
            elif t == E.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
                a = e.child_workflow_execution_started_event_attributes
                if a.workflow_type.name == "CaseInvestigationWorkflow":  # abandoned: runs on alone
                    by_id[a.initiated_event_id]["end"] = at
            elif t == E.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_COMPLETED:
                a = e.child_workflow_execution_completed_event_attributes
                by_id[a.initiated_event_id] |= {"end": at, "result": await self._payload(a.result)}
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
