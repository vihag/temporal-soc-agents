# Code tour

A 15-minute walk through the repo, for a Temporal reviewer reading offline and for Vihag screen-sharing on the call. Read **these six stops, in this order**. Links go to exact lines on `main`.

One Workflow per alert. Four agent classes the Workflow calls. Every model call, tool call and side effect is an Activity. Splunk and SOAR are small mocks. Grok is real only when `GROK_CLIENT=http` and a key are set.

---

## 1. `src/soc/models.py` — the vocabulary (about 2 minutes)

- [`Status`](src/soc/models.py#L16): the ten case states. The Workflow's `_enter()` (stop 2) is the only writer.
- [`CaseInput`](src/soc/models.py#L32): the Workflow input. Runtime knobs ride here because Workflow code must not read env vars.
- [`DecisionRequest`](src/soc/models.py#L88) (the Update payload) and [`Case`](src/soc/models.py#L127) (everything the Workflow knows; what the `get_case` Query returns).
- [`RoutingPolicy.decide`](src/soc/models.py#L174): a pure rule (autonomous / collaborative / review), small enough to read in one breath.

Plain dataclasses, no I/O. Temporal's default converter carries them through Activities, Queries and Updates unchanged.

---

## 2. `src/soc/workflow.py` — the happy path *is* the state machine (about 4 minutes)

Linger here.

- [`run()`](src/soc/workflow.py#L42): read it top to bottom. `fetch_alert` → `TriageAgent` → [`RoutingPolicy`](src/soc/workflow.py#L52) → auto-resolve / review / [investigate → propose → wait for the analyst (rework loop, cap 2)](src/soc/workflow.py#L61) → [`soar_submit`](src/soc/workflow.py#L74). Every `self._enter(Status.X, ...)` line is a state change, in order.
- [`_investigate()`](src/soc/workflow.py#L79): Evidence and Threat agents in parallel with plain `asyncio.gather`. No child Workflows.
- [`_wait_for_analyst()`](src/soc/workflow.py#L89): `notify`, then `wait_condition` with a 2-minute durable timer. On [timeout](src/soc/workflow.py#L100): `ESCALATED`, notify again, keep waiting. No worker thread is held.
- [`_enter()`](src/soc/workflow.py#L109): the one place status changes. It also appends the timeline.
- Queries [`get_case`](src/soc/workflow.py#L116) and [`approval_pending`](src/soc/workflow.py#L120). Update [`decide`](src/soc/workflow.py#L124) returns once the Workflow has acted. Its [validator](src/soc/workflow.py#L130) rejects a non-analyst or an untimely call *before* anything reaches history.

Not here: HTTP, the Grok SDK, a transitions table, 409 guards, child Workflows. Workflow ID is `case-{alert_id}`, so a second start for the same alert is refused.

Contrast: forge-legion's `services/case_api/state.py` (a `TRANSITIONS` table plus 409s) is replaced by this one method.

---

## 3. `src/soc/agents.py` — four agents, one loop (about 3 minutes)

- [`Agent._converse()`](src/soc/agents.py#L33): the Temporal AI-cookbook loop, in one place. Call the `llm_create` Activity. If the model asks for a tool, run the `run_tool` Activity and append both messages in Chat Completions format. Repeat until it answers. On the last allowed turn no tools are offered.
- [`TriageAgent`](src/soc/agents.py#L66) and [`DispositionAgent`](src/soc/agents.py#L101): the cookbook *tool-call* recipe (at most one tool).
- [`EvidenceAgent`](src/soc/agents.py#L79) and [`ThreatAgent`](src/soc/agents.py#L90): the cookbook *agentic loop*, capped by `MAX_AGENT_STEPS` (6).

Subclasses only declare instructions, tool names, answer keys and whether they loop. Understand `_converse` and you understand every agent. They run inside the Workflow, so they may only call Activities.

---

## 4. `src/soc/activities.py` + `src/soc/llm.py` — the I/O boundary (about 3 minutes)

- Activities: [`fetch_alert`](src/soc/activities.py#L43), [`llm_create`](src/soc/activities.py#L52), [`run_tool`](src/soc/activities.py#L78), [`notify`](src/soc/activities.py#L87), [`soar_submit`](src/soc/activities.py#L93).
- [`OPTIONS`](src/soc/activities.py#L115): every timeout and retry policy in one table. [`call()`](src/soc/activities.py#L124) is the Workflow-side helper that applies it.
- `llm_create` **validates** each reply (required answer keys, only offered tools). A bad sample raises, so Temporal retries the call. That is the self-healing loop.
- [`GrokClient`](src/soc/llm.py#L46): OpenAI-compatible Chat Completions pointed at xAI, with [`max_retries=0`](src/soc/llm.py#L49) so Temporal, not the SDK, owns retries. [429 → `next_retry_delay=Retry-After`](src/soc/llm.py#L73); other 4xx → non-retryable (the case goes to `NEEDS_REVIEW`). [`FixtureLLMClient`](src/soc/llm.py#L27) replays recorded turns; [`make_llm_client`](src/soc/llm.py#L103) picks fixture unless a key *and* `GROK_CLIENT=http` are set.
- [`_maybe_fail`](src/soc/activities.py#L33): fixture fault injection (timeouts, a garbled model answer, a lost SOAR response), so the demo shows real retries.

---

## 5. `src/soc/tools.py`, `src/soc/soar.py`, `services/` — the outside world (about 1 minute)

- [`Tool`](src/soc/tools.py#L12): name, description, params → JSON schema; `run()` calls Mock Splunk. Five subclasses and an explicit [`TOOLS`](src/soc/tools.py#L65) dict. No plugin loader.
- [`HttpSoarClient`](src/soc/soar.py#L18) sends `Idempotency-Key: soar-submit-{case_id}`. [`SoarStore`](services/mock_soar.py#L16) is unique on `case_id`: a retry after a lost response returns the same ticket with `created=false`. That is the other half of exactly-once.

---

## 6. `src/soc/api.py` + `src/soc/ui/index.html` — the analyst app (about 2 minutes)

- `api.py` is a stateless Temporal client. [`decide`](src/soc/api.py#L128) is an Update; a validator refusal becomes HTTP 409. [`/history`](src/soc/api.py#L140) and [`HistoryBars`](src/soc/api.py#L162) turn the **real** event history into Gantt bars: decoded inputs and outputs, failed attempts, timers, Updates. [`/demo/{scenario}`](src/soc/api.py#L265) runs a simulation in the background.
- `ui/index.html`, one file and plain JS. The **Cases** tab has the queue, the detail page and Approve/Reject. **Self demo** has the stepper, `workflow.py` with the [executing line](src/soc/ui/index.html#L192) highlighted, and the live Gantt. Deep detail opens in a *What Temporal did here* drawer, the ClickHouse demo's "SQL + EXPLAIN" pattern.

Supporting files: [`simulate.py`](src/soc/simulate.py#L39) (five named scenarios, a [`Narrator`](src/soc/simulate.py#L77) and [`run_simulation`](src/soc/simulate.py#L116), also used by pytest), [`worker.py`](src/soc/worker.py#L29), [`settings.connect`](src/soc/settings.py#L52) (Temporal env config, so local or Cloud is configuration only), and `cli.py`.

---

## Tests (read after the tour)

| Test | Proves |
| --- | --- |
| `tests/test_simulations.py` | All five demo scenarios end to end on the time-skipping server: the 2-minute timer, escalation, reject → re-investigate, retries, one SOAR ticket |
| `tests/test_workflow.py` | Review routing, a fatal model error → `NEEDS_REVIEW`, duplicate case ID, Update validator, deterministic **replay**, **worker restart** mid-case with no Activity re-run |
| `tests/test_agents.py` | LLM **self-heal**, the Evidence loop really calls tools over HTTP, **parallel** agents, Grok 429/4xx/tool-call mapping (faked HTTP, no live call) |
| `tests/test_soar.py`, `tests/test_api.py` | SOAR uniqueness and 409; history → Gantt bars; decide on a closed case → 409 |

## If they ask "where is X?"

| Question | Go to |
| --- | --- |
| Where does status change? | [`_enter()`](src/soc/workflow.py#L109), called only from the happy path in `run()` |
| Where is the state machine table? | Nowhere. Contrast forge-legion `services/case_api/state.py` |
| Where are child Workflows? | Nowhere, on purpose. Parallelism is `asyncio.gather` in `_investigate()` |
| Where are retries configured? | [`OPTIONS`](src/soc/activities.py#L115) |
| Where is LiteLLM? | Nowhere, on purpose: one OpenAI-compatible client to xAI |
| How do I start it? | `README.md` |
| How do we demo it? | `DEMO.md` |
