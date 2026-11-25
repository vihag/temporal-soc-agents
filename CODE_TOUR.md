# Code tour

A 15-minute walk through the repo, for a Temporal reviewer reading offline and for Vihag screen-sharing on the call. Read **these six stops, in this order**. Do not start in `tests/` or `compose.yml`.

Line numbers will be filled in when the code lands (FORGE T4). Until then, use the symbols.

The app is a SOC case investigation on the Temporal Python SDK. One Workflow per alert. Specialist "agents" are classes the Workflow calls. Splunk and SOAR are mocks. Grok is real only when a key is set.

---

## 1. `src/soc/models.py` — the domain (about 2 minutes)

Look at:

- `Case`, `Evidence`, `Finding`, `Disposition`, `DecisionRequest`, `ApprovalStatus`
- `Status` — the enum. Only `CaseInvestigationWorkflow._enter()` writes it; there is no transition table
- `RoutingPolicy.decide()` — pure function, port of forge-legion's routing rules; the model's `recommended_route` is advisory

This is the vocabulary the rest of the code uses. There is no `state.py` and no `check_transition()`.

---

## 2. `src/soc/workflow.py` — the happy path (about 4 minutes)

This is the file to linger on.

- `CaseInvestigationWorkflow.run(CaseInput)` — read it top to bottom. That *is* the case lifecycle: every `self._enter(Status.X, ...)` line is a state change, in happy-path order.
- `fetch_alert` → `TriageAgent().run()` → `RoutingPolicy` → either auto-resolve, needs-review, or `asyncio.gather(EvidenceAgent().run(), ThreatAgent().run())` → `DispositionAgent().run()` → `wait_condition` on an Update → `soar_submit`.
- Query `get_case` returns the `Case` for the UI and CLI. Query `approval_pending` returns `ApprovalStatus(pending, disposition)`.
- Update `decide(DecisionRequest)`: its validator rejects a non-analyst or a call made at the wrong time, and it returns the case after the Workflow has acted on the decision. Reject sends the case back to investigation, at most twice.
- The 2-minute approval timer and the `ESCALATED` path.

Notice what is **not** here: child Workflows, HTTP, the Grok SDK, a status enum mutation API.

Workflow ID is `case-{alert_id}`. A second start of the same alert is `WorkflowAlreadyStartedError`.

---

## 3. `src/soc/agents.py` — the four agents (about 3 minutes)

One base class, four subclasses, one file.

- `Agent` — shared mechanics. `run()` calls the `llm_create` Activity and, when the model asks, a named tool Activity. It appends to conversation history. It does not open sockets or read the clock.
- `TriageAgent`, `DispositionAgent` — `loop=False`. Cookbook "tool-calling agent" (at most one tool, then a structured result).
- `EvidenceAgent`, `ThreatAgent` — `loop=True`, `max_steps=6`. Cookbook "agentic loop". Tools are an explicit list of names on the class, not a plugin registry.

If you understand `Agent.run()`, you understand every specialist.

---

## 4. `src/soc/activities.py` and `src/soc/llm.py` — the I/O boundary (about 2 minutes)

- `activities.py` is thin on purpose: `llm_create`, `run_tool`, `fetch_alert`, `notify`, `soar_submit`. Each constructs a client or a tool and calls it.
- `llm.py` — `LLMClient` (OpenAI-compatible Chat Completions, `base_url` → xAI, **`max_retries=0`**) and `FixtureLLMClient`. Temporal, not the vendor SDK, owns retries.
- `llm_create` validates every reply (required answer keys, only offered tools). A bad reply raises, and Temporal retries the call: that is self-healing, capped at 5 attempts. `GrokClient` maps 429 to `next_retry_delay=Retry-After`. Other 4xx become non-retryable, so the Workflow flags `NEEDS_REVIEW`. Tokens and the model that actually answered are logged on every call.
- `FixtureLLMClient` is used unless `GROK_CLIENT=http` **and** `XAI_API_KEY` is set. Tests and CI never call Grok.

This is why the LLM is an Activity: Workflows must be deterministic; a model call is I/O.

---

## 5. `src/soc/tools.py` and `src/soc/soar.py` — externals (about 2 minutes)

- `Tool` base class plus five tools (`search_evidence`, `list_alert_evidence`, `lookup_asset`, `enrich_ioc`, `lookup_mitre`). Each declares a `name`, a description and its params. The base class builds the JSON schema and `run()` queries Mock Splunk over HTTP. `TOOLS` is an explicit dict of the five instances, not a plugin loader.
- `SoarClient` / `HttpSoarClient`. `soar_submit` uses idempotency key `soar-submit-{alert_id}`. Mock SOAR is unique on `case_id` and will return the original ticket if the Activity is retried after a timeout-on-success.

Two compose services, `services/mock_splunk.py` and `services/mock_soar.py`, implement those HTTP APIs. You do not need to read them unless you are following a tool call all the way down.

---

## 6. `src/soc/api.py` and `src/soc/ui/index.html` — the analyst app (about 2 minutes)

- `api.py` — FastAPI. List cases, start, get a case (Workflow Query), `decide` (Workflow Update; a validator refusal becomes HTTP 409), `/history` (`HistoryBars` turns the raw event history into Gantt bars with decoded inputs/outputs and retry attempts), `/source` (the real code the UI shows), and `/demo/{scenario}` (runs a simulation in the background). No business sequencing here.
- `ui/index.html` — one file, plain JS. **Cases** tab: queue, case detail (timeline, findings, evidence), Approve / Reject. **Self demo** tab: a scenario picker, a *runs → breaks → recovers* stepper, `workflow.py` with the executing line highlighted, and a Gantt polled every second from the real event history. Deep detail is a right-hand **drawer** ("What Temporal did here"), the same idea as the ClickHouse demo's "SQL + EXPLAIN" panel: the main page stays clean; the SA opens a drawer when they want the Temporal story.

`simulate.py` is the self-driving demo: five named scenarios that start a real Workflow, play the analyst, and narrate the timeline plus what Temporal did (from the real event history). The pytest suite runs the same scenarios.

`worker.py` and `cli.py` are short process entrypoints. Read them only if you want to see how the Worker or a CLI start is wired (`ClientConfig` for local vs Cloud).

---

## If they ask "where is X?"

| Question | Go to |
| --- | --- |
| Where does status change? | It does not, as a write. `Case.status` is derived; `run()` is the lifecycle |
| Where is the state machine table? | Nowhere. Contrast forge-legion `services/case_api/state.py` |
| Where are child Workflows? | Nowhere, on purpose. Parallelism is `asyncio.gather` in `workflow.py` |
| Where is LiteLLM? | Nowhere, on purpose |
| How do I start it? | `README.md` |
| How do we demo it? | `DEMO.md` |
