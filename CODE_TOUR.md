# Code tour

A 15-minute walk through the repo, for a Temporal reviewer reading offline and for Vihag screen-sharing on the call. Read **these seven stops, in this order**. Links go to exact lines on `main`.

Alerts arrive on a queue. One intake Workflow turns each into one case Workflow. Agent classes do the investigation. Analysts act through validated Updates. Every model call, tool call, queue poll and side effect is an Activity. Splunk, SOAR, Grok and the queue are small local mocks; Grok is real only when `GROK_CLIENT=http` and a key are set.

---

## 1. `src/soc/models.py`: the vocabulary (about 2 minutes)

- [`ROSTER`](src/soc/models.py#L15) and [`Status`](src/soc/models.py#L18): the analysts on shift and the case states (incl. `ASSIGNED`, `PARKED`). The Workflow's `_enter()` is the only writer of status.
- [`CaseInput`](src/soc/models.py#L36): the Workflow input (scenario, assignee, alert id, SLA). Runtime knobs ride here because Workflow code must not read env vars.
- The analyst Update payloads: [`DecisionRequest`](src/soc/models.py#L94), [`ReassignRequest`](src/soc/models.py#L104), [`ParkRequest`](src/soc/models.py#L111), [`AskRequest`](src/soc/models.py#L118). Then [`IntakeState`](src/soc/models.py#L160) (what intake carries across continue-as-new) and [`Case`](src/soc/models.py#L201) (what the `get_case` Query returns).

Plain dataclasses, no I/O.

---

## 2. `src/soc/intake.py`: the queue becomes cases (about 2 minutes)

- [`IntakeWorkflow.run`](src/soc/intake.py#L32): poll (`receive_alerts`), open a case per message, ack, sleep, and every N polls [`continue_as_new`](src/soc/intake.py#L41) with the same counters.
- [`_open_case`](src/soc/intake.py#L43): `start_child_workflow(..., id=f"case-{alert_id}", ABANDON, REJECT_DUPLICATE)`. A duplicate alert raises `WorkflowAlreadyStartedError` and is counted, not re-opened. Round robin assigns the analyst.
- [`ensure_intake`](src/soc/intake.py#L79): every worker can call it; `USE_EXISTING` makes it idempotent.

A queue outage is just `receive_alerts` retrying forever (stop 5). Alerts stay in the queue.

---

## 3. `src/soc/workflow.py`: the case lifecycle *is* the state machine (about 4 minutes)

Linger here.

- [`run()`](src/soc/workflow.py#L60) → [`_lifecycle()`](src/soc/workflow.py#L67): read it top to bottom. `fetch_alert` → assigned → `TriageAgent` → routing → auto-resolve / review / investigate → propose → wait for the analyst (rework loop) → `soar_submit`. `run()` ends with `wait_condition(all_handlers_finished)` so no Update result is lost.
- [`_investigate()`](src/soc/workflow.py#L106): Evidence and Threat agents in parallel with `asyncio.gather`.
- [`_wait_for_analyst()`](src/soc/workflow.py#L116): the approval SLA is a durable timer ([`timeout_summary="approval SLA"`](src/soc/workflow.py#L137)). On timeout: `ESCALATED`, page the lead, keep waiting. A parked case waits in [`_stay_parked()`](src/soc/workflow.py#L149) on its own `park timer` instead, so the SLA is paused.
- Updates, each with a validator that refuses bad requests *before* they reach history: [`decide`](src/soc/workflow.py#L190) (assignee only), [`reassign`](src/soc/workflow.py#L208), [`park`](src/soc/workflow.py#L225) / [`unpark`](src/soc/workflow.py#L242), and [`ask`](src/soc/workflow.py#L255), which runs a child [`AskAgentWorkflow`](src/soc/workflow.py#L293) and attaches its answer, evidence and findings to the case.

---

## 4. `src/soc/agents.py`: agents, one loop (about 2 minutes)

- [`Agent._converse()`](src/soc/agents.py#L43): the Temporal AI-cookbook loop, in one place. Call the `llm_create` Activity; if the model asks for a tool, run the `run_tool` Activity; repeat until it answers.
- [`TriageAgent`](src/soc/agents.py#L76), [`EvidenceAgent`](src/soc/agents.py#L89), [`ThreatAgent`](src/soc/agents.py#L100), [`DispositionAgent`](src/soc/agents.py#L111) and the analyst-facing [`InvestigatorAgent`](src/soc/agents.py#L122) only declare instructions, tools and answer keys.

---

## 5. `src/soc/activities.py` + `src/soc/llm.py`: the I/O boundary (about 3 minutes)

- Activities: [`fetch_alert`](src/soc/activities.py#L52), [`llm_create`](src/soc/activities.py#L62), [`run_tool`](src/soc/activities.py#L88), [`notify`](src/soc/activities.py#L97), [`soar_submit`](src/soc/activities.py#L103), [`receive_alerts`](src/soc/activities.py#L116), [`ack_alerts`](src/soc/activities.py#L126).
- [`OPTIONS`](src/soc/activities.py#L146): every timeout and retry policy in one table (queue Activities retry forever). [`call()`](src/soc/activities.py#L157) applies it.
- `llm_create` validates each reply; a bad sample raises, so Temporal retries it (self-healing).
- [`GrokClient`](src/soc/llm.py#L22): one OpenAI-compatible client. It talks to xAI when live, otherwise to Mock Grok, so fixture mode goes through the same HTTP path and the chaos panel can break it. `max_retries=0`, so Temporal owns retries; 429 → `next_retry_delay=Retry-After`.

---

## 6. `services/` + `src/soc/chaos.py`: the outside world, and breaking it (about 1 minute)

- [`JsonHandler`](services/common.py#L30): every mock has `POST /admin/outage {"seconds", "code"}`. [`AlertQueue`](services/mock_queue.py#L21) is SQS-like (receive, visibility timeout, delete); during an outage, sends still work, so alerts buffer.
- [`mock_grok.reply`](services/mock_grok.py#L20) replays recorded turns per scenario/agent/step. The ask agent picks a script by keyword.
- [`Chaos`](src/soc/chaos.py#L26): outage switches, [`SEED`](src/soc/chaos.py#L14) (8 alerts, one duplicate), and the SLA-breach alert. [`WorkerSupervisor`](src/soc/worker.py#L37) stops and restarts the in-process worker.
- [`SoarStore`](services/mock_soar.py#L16) is unique on `case_id`: the other half of exactly-once.

---

## 7. `src/soc/api.py` + `src/soc/ui/index.html`: the console (about 2 minutes)

- `api.py` is a stateless Temporal client. [`act`](src/soc/api.py#L159) maps decide / reassign / park / unpark / ask to Updates (validator refusal → HTTP 409; `ask` returns once *accepted*). [`HistoryBars`](src/soc/api.py#L260) turns the **real** event history into Gantt bars: Activities with failed attempts, timers labelled by their summary, Updates by name, child Workflows, duplicate rejects. Plus [`/api/intake`](src/soc/api.py#L194), [`/api/chaos`](src/soc/api.py#L220) and [`/api/seed`](src/soc/api.py#L215).
- `ui/index.html`, one file and plain JS: **Cases** (an analyst switcher, my/all, filters, actions, the live timeline on the right), **Alert intake**, **Self demo**, and the **⚡ Chaos** bar. Every bar opens a *What Temporal did here* drawer.

Supporting files: [`simulate.py`](src/soc/simulate.py#L39) (five narrated scenarios, also used by pytest), [`settings.connect`](src/soc/settings.py#L56) (local or Cloud is configuration only) and [`cli.py`](src/soc/cli.py#L141).

---

## Tests (26, time-skipping, no live Grok)

| Test | Proves |
| --- | --- |
| `tests/test_v2.py` | Intake: child per alert, round robin, duplicate no-op, a queue outage retried and drained; continue-as-new keeps counters; reassign; the park timer pauses the SLA and auto-resumes, plus unpark; ask runs a child Workflow and attaches the exemption finding; a model outage heals by retry |
| `tests/test_simulations.py` | All five demo scenarios end to end: the SLA timer, escalation, reject → re-investigate, retries, one SOAR ticket |
| `tests/test_workflow.py` | Review routing, a fatal model error → `NEEDS_REVIEW`, duplicate case ID, Update validator, deterministic **replay**, **worker restart** mid-case |
| `tests/test_agents.py` | LLM self-heal, tool calls over HTTP, parallel agents, Grok 429/4xx/tool-call mapping (faked HTTP) |
| `tests/test_soar.py`, `tests/test_api.py` | SOAR uniqueness; history → Gantt bars; every analyst action through the API and its validator |

## If they ask "where is X?"

| Question | Go to |
| --- | --- |
| Where does status change? | [`_enter()`](src/soc/workflow.py#L167) |
| Where are retries configured? | [`OPTIONS`](src/soc/activities.py#L146) |
| How are duplicates prevented? | [`_open_case`](src/soc/intake.py#L43): Workflow id `case-{alert_id}` |
| Where are child Workflows? | Intake → case (ABANDON) and case → `AskAgentWorkflow` |
| How do I start it? | `README.md` |
| How do we demo it? | `DEMO.md` |
