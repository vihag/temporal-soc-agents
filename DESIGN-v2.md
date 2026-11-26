# DESIGN v2 — from scenario simulator to a small real SOC console

v1 (`main` @ `3b122db`) stays the base: one `CaseInvestigationWorkflow` per alert, four agents, Activities for all I/O, fixture mode by default. v2 adds the parts a SOC actually runs on: alert intake from a queue, analysts and assignment, parking, analyst-driven follow-up questions, and a chaos panel that breaks things on purpose. Same guardrails: lean, readable, strong OOP, $0, no secrets, no live Grok.

## 1. Pieces and the Temporal primitive each one shows

| # | Piece | What it is | Temporal primitive it showcases |
| --- | --- | --- | --- |
| 1 | **Mock queue** `services/mock_queue.py` | A tiny SQS-like HTTP service: `send`, `receive` (visibility timeout), `delete`, `stats`. Messages stay until deleted | (external system) |
| 2 | **`IntakeWorkflow`** `src/soc/intake.py` | One long-running Workflow (`alert-intake`). It loops: `receive_alerts` Activity → start one case per message → `ack_alerts` → durable `workflow.sleep`. After N polls it **continues as new**, carrying its state | Long-running entity Workflow, **child Workflows** (`ParentClosePolicy.ABANDON`), **Workflow ID uniqueness** (`case-{alert_id}` + `REJECT_DUPLICATE`: a duplicate alert is a recorded no-op), **unlimited Activity retries** (queue outage: alerts wait in the queue, nothing is lost, and they drain on recovery), **continue-as-new** |
| 3 | **Roster + assignment** | Three seeded analysts on shift (`vihag`, `aisha`, `ben`). Intake assigns **round robin**, and the counter survives continue-as-new. Update `reassign` (e.g. urgent leave): the new analyst sees the full timeline and continues at the same step. Only the assignee may decide or park | **Update + validator** (only a rostered analyst, never a no-op, never a closed case), Workflow state as the single source of truth |
| 4 | **Park / snooze** | Update `park(seconds)` while the case waits on the analyst. Status `PARKED`. It resumes when the park timer fires or on Update `unpark`. The approval SLA timer does not run while the case is parked | **Durable timers** + `wait_condition` with a timeout, racing a timer against a human |
| 5 | **Ask-an-agent** | From the case page: "pull 7 more days of Splunk logins for host X", "this week's exemption tickets for user Y". Update `ask` starts **`AskAgentWorkflow`** (a child), an agentic loop over tools including two new ones: `search_logins` and `lookup_exemptions` (a mock exemption-ticket endpoint). Its findings and evidence attach to the case timeline | **Child Workflow** with its own history, an **async Update handler** that awaits the child, `workflow.all_handlers_finished` (a case never closes under a running question) |
| 6 | **Mock Grok** `services/mock_grok.py` | An OpenAI-compatible `/v1/chat/completions` that replays the recorded fixture turns. **Fixture mode now goes through the real `GrokClient` HTTP path.** It replaces `FixtureLLMClient` (fewer lines) | Makes model failures (5xx, 429 + `Retry-After`) real, so self-healing and backoff are visible |
| 7 | **Chaos panel** (UI + `soc chaos …`) | Queue down N s · Splunk down N s · Grok down N s · Grok rate-limited N s · worker down N s (in-process worker in `api --with-worker`; with Compose, `docker compose kill worker`) · inject an SLA-breach alert (20 s approval SLA) | Activity retries/backoff, `next_retry_delay`, worker crash → replay, timer → `ESCALATED`. Each is visible on the right-hand Temporal timeline |
| 8 | **Seed + runbook** | `soc seed` pushes 8 alerts, including one duplicate. `DEMO.md` becomes a step-by-step runbook | — |

All mocks share one outage switch: `POST /admin/outage {"seconds": N, "code": 503|429}` on `services/common.JsonHandler`.

## 2. State machine changes

Two new statuses. As in v1, status is written only by `_enter()` in `workflow.py`.

```
NEW → ASSIGNED → TRIAGED → (AUTO_RESOLVED | NEEDS_REVIEW | INVESTIGATING)
INVESTIGATING → DISPOSITION_PROPOSED ⇄ PARKED          (park / unpark / park timer)
DISPOSITION_PROPOSED → ESCALATED                        (SLA timer)  ⇄ PARKED
DISPOSITION_PROPOSED|ESCALATED|PARKED → APPROVED → SUBMITTED
                                       → REJECTED → INVESTIGATING (cap 2)
```

- `ASSIGNED`: right after the alert is fetched; the assignee comes from intake (round robin) or the CLI.
- `PARKED`: only while the case waits on the analyst. Unpark returns to the previous waiting status.
- `reassign` and `ask` add timeline entries but do **not** change status. A reassigned case continues exactly where it was.

New Updates on `CaseInvestigationWorkflow`: `reassign`, `park`, `unpark`, `ask`, each with a validator. `decide` now also requires `analyst_id == assignee`.

## 3. Files and LOC estimate (Python, excluding tests and UI)

| File | v1 | v2 est. | Change |
| --- | ---: | ---: | --- |
| `models.py` | 191 | ~260 | roster, new Update payloads, queue message, ask types, 2 statuses |
| `workflow.py` | 138 | ~230 | reassign/park/unpark/ask, `AskAgentWorkflow` |
| `intake.py` | — | ~110 | `IntakeWorkflow`, `receive_alerts` / `ack_alerts`, `ensure_intake` |
| `agents.py` | 109 | ~125 | `InvestigatorAgent` |
| `activities.py` | 126 | ~125 | `fetch_alert(CaseInput)` |
| `llm.py` | 106 | ~80 | `FixtureLLMClient` removed (mock Grok) |
| `tools.py` | 68 | ~85 | `search_logins`, `lookup_exemptions` |
| `chaos.py` | — | ~50 | `Chaos`: outage switches + seed + SLA-breach alert |
| `cli.py`, `simulate.py`, `settings.py`, `worker.py`, `scenarios.py`, `soar.py` | 487 | ~560 | `seed`, `chaos`, roster, supervised worker |
| `services/*` | 156 | ~300 | `mock_queue.py`, `mock_grok.py`, outage switch |
| **Total** | **1,385** | **~1,950** | cap ~2,200 |

`api.py` (+ cases filters, actions, chaos, intake timeline) and `ui/index.html` are outside the budget.

## 4. UI

A SOC console, one HTML file, plain JS:
- **Top bar**: "Viewing as" analyst switcher (no auth), Seed alerts, **Chaos** panel, Temporal UI link.
- **Left**: case queue with status and assignee filters, plus a *My cases* toggle.
- **Middle**: case detail with the timeline (each step has *What Temporal did here*), findings, evidence and questions, and actions: Approve / Reject, Park (30 s / 2 min / 1 h) / Unpark, Reassign, Ask agent (free text + suggestions).
- **Right**: the **Temporal timeline**, a live Gantt of the selected case's real history (or the intake Workflow's), with drawers.
- The **Self demo** tab stays.

## 5. Tests (time-skipping)

Intake: start, duplicate no-op, and a queue outage that drains on recovery. Continue-as-new carries the round-robin counter. Reassign: the old assignee can no longer decide. Park: auto-resume on the timer, unpark, no escalation while parked. Ask: the child Workflow attaches findings, using the exemption tool. Grok outage: the case heals. All v1 tests stay green.

## 6. Out of scope for this slice (next discussion)

Schedules, search attributes / visibility queries for the queue, Worker Versioning / patching, Nexus, multi-namespace, Temporal Cloud deployment, auth/RBAC, Codec Server / payload encryption, metrics/OTel.
