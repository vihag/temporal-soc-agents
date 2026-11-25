# Code tour

A 15-minute walk through the repo, for a Temporal reviewer reading offline and for Vihag screen-sharing on the call. Read **these six stops, in this order**. Do not start in `tests/` or `compose.yml`.

Line numbers will be filled in when the code lands (FORGE T4). Until then, use the symbols.

The app is a SOC case investigation on the Temporal Python SDK. One Workflow per alert. Specialist "agents" are classes the Workflow calls. Splunk and SOAR are mocks. Grok is real only when a key is set.

---

## 1. `src/soc/models.py` — the domain (about 2 minutes)

Look at:

- `Case`, `Evidence`, `Finding`, `Disposition`, `Decision`, `CaseView`
- `Case.status` — a **derived** property, not a field written by a transition table
- `RoutingPolicy.decide()` — pure function, port of forge-legion's routing rules; the model's `recommended_route` is advisory

This is the vocabulary the rest of the code uses. There is no `state.py` and no `check_transition()`.

---

## 2. `src/soc/workflow.py` — the happy path (about 4 minutes)

This is the file to linger on.

- `CaseInvestigationWorkflow.run(alert_id)` — read it top to bottom. That *is* the case lifecycle.
- `fetch_alert` → `TriageAgent().run()` → `RoutingPolicy` → either auto-resolve, needs-review, or `asyncio.gather(EvidenceAgent().run(), ThreatAgent().run())` → `DispositionAgent().run()` → `wait_condition` on an Update → `soar_submit`.
- Query `get_case` — returns `CaseView` for the UI and CLI.
- Update `decide(decision, rationale, analyst_id)` — rejects a non-analyst or a call made at the wrong time; returns the new view.
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
- Invalid structured output is retried, capped. 429 uses `Retry-After`. Other 4xx become a non-retryable error and the Workflow flags `NEEDS_REVIEW`.

This is why the LLM is an Activity: Workflows must be deterministic; a model call is I/O.

---

## 5. `src/soc/tools.py` and `src/soc/soar.py` — externals (about 2 minutes)

- `Tool` base class plus five tools (`search_evidence`, `list_alert_evidence`, `lookup_asset`, `enrich_ioc`, `lookup_mitre`). Each has a `name`, a JSON schema, and `run()`. They talk to Mock Splunk over HTTP.
- `SoarClient` / `HttpSoarClient`. `soar_submit` uses idempotency key `soar-submit-{alert_id}`. Mock SOAR is unique on `case_id` and will return the original ticket if the Activity is retried after a timeout-on-success.

Two compose services, `services/mock_splunk.py` and `services/mock_soar.py`, implement those HTTP APIs. You do not need to read them unless you are following a tool call all the way down.

---

## 6. `src/soc/api.py` and `src/soc/ui/index.html` — the analyst app (about 2 minutes)

- `api.py` — FastAPI. List cases, get a case (Workflow Query), `decide` (Workflow Update), and `/insight/...` for the drawers (a history excerpt, retry count, LLM I/O, Temporal UI link). No business sequencing here.
- `ui/index.html` — case queue, case detail (timeline, findings, evidence), Approve / Reject. Deep detail is a right-hand **drawer** ("What Temporal did here"), the same idea as the ClickHouse demo's "SQL + EXPLAIN" panel: the main page stays clean; the SA opens a drawer when they want the Temporal story.

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
