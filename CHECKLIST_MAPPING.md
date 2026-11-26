# Challenge checklist → showcase

How this take-home maps the Temporal SA exercise, the interview, the capabilities an SA will probe, Temporal's [AI pillars](https://temporal.io/solutions/ai), and the [AI Cookbook](https://docs.temporal.io/ai/cookbook/) recipes we follow — without a sprawling tree.

The build is a **lean, readable** standalone Python SDK app that Temporalizes the FORGE × LEGION SOC case workflow ([`vihag/forge-legion`](https://github.com/vihag/forge-legion) `@` `08deab4`) as durable AI agents. Reviewers read the repo offline; Vihag screen-shares it live. Target: grasp the code in about 15 minutes. Start at [`CODE_TOUR.md`](CODE_TOUR.md).

This file is the source of truth for the mapping; it is also §0 of [`DESIGN.md`](DESIGN.md). Later sections and FORGE phases cite these IDs.

Priority: **must** is the 5–6 hour cut and the $50 cap. **should** ships if the cut allows. **nice** is follow-on.

Evidence paths are the intended layout (repo name: `temporal-soc-agents`). They appear when the matching FORGE phase lands. Diagrams in `diagrams/` ship with this design package.

| ID | Checklist item | How this SOC build showcases it | Demo moment | Evidence artifact | Priority |
| --- | --- | --- | --- | --- | --- |
| **A. Assignment** | | | | | |
| A1 | Choose a use case and Temporalize it | Agentic SOC case investigation from Vihag's FORGE × LEGION work: alert → Grok triage → Evidence (and optional Threat) loop → disposition → analyst approval → exactly-once SOAR | Open with the business problem and why a small app beats a bot army for this interview | `DESIGN.md` §1; `services/case_api/state.py`, `docs/bots.md` | must |
| A2 | Prefer a Temporal SDK | Temporal Python SDK in a short, class-based tree: one Workflow, one Agent hierarchy, one worker | Walk `CaseInvestigationWorkflow.run` then the four Agent subclasses | `src/soc/workflow.py`, `src/soc/agents.py` | must |
| A3 | Diagrams | Four diagrams: compose architecture, derived states, sequence (parallel agents + Update + timeout), failure/retry | Flip through the PNGs, then the live Workflow history | `diagrams/*.png` | must |
| A4 | About 5–6 hours; not completeness | Tight scope, LOC budget (<1500 Python, excl. tests/UI), $50 cap, four implementation PRs | Say what we cut (child Workflows, LiteLLM, tool plugin registry, Case API rebuild) | `DESIGN.md` §11, §13, §14 | must |
| A5 | Mock externals | Mock Splunk and Mock SOAR as small compose services. Real xAI Grok when a key is set; fixture is the default for tests/CI | Run `hitl-confirm-01` on fixtures; optionally one live Grok triage | `services/mock_splunk.py`, `services/mock_soar.py`, `src/soc/llm.py` | must |
| A6 | Compiles and runs | `docker compose up` (or a 3-terminal fallback). Tests run with fixtures, no API key | From a clean checkout, follow the README, start a case, reach the approval wait | `README.md`; `tests/` | must |
| A7 | Minimal README + start command; not production-ready | README quickstart, plus `CODE_TOUR.md` and `DEMO.md` for reviewers. No k8s | Follow README, then CODE_TOUR in the repo | `README.md`, `CODE_TOUR.md`, `DEMO.md` | must |
| A8 | Public GitHub repo preferred | New public repo `vihag/temporal-soc-agents`. Created; phases T1–T4 merged as PRs #1–#4 | Paste the repo URL at the start of the review | https://github.com/vihag/temporal-soc-agents | must |
| A9 | AI-written code is allowed; explain every line | Small OOP tree. FORGE implements; Vihag reviews every PR and can walk Workflow, Agent, LLMClient, Update, SOAR key | Interviewer points at any file; Vihag walks it via CODE_TOUR order | `CODE_TOUR.md` | must |
| A10 | Stay within a $50 total spend cap | Fixture-default tests; cheap current Grok model; local Temporal for build; Cloud only for the interview using existing credits; four pinned cheap Cursor runs | Mention the cost table if asked; live Grok is a handful of cases, not a soak | `DESIGN.md` §11 | must |
| **B. Interview format** | | | | | |
| B1 | 20–30 min review: how Temporal addresses distributed-systems challenges | `DEMO.md` is ordered around recovery, retries, exactly-once SOAR, HITL timers, parallelism — not a SOC feature tour. Deep Temporal detail lives in UI drawers | Run `DEMO.md` in that order; open one "What Temporal did here" drawer | `DEMO.md` | must |
| B2 | 10–15 min Q&A: app dev and distributed systems | Short answers: determinism, Activity vs Workflow, idempotency, one task queue, Cloud vs local, Update vs Signal, why no child Workflows | Leave time; do not overrun the demo | `DEMO.md` talking points | must |
| B3 | 10 min candidate Q&A | Two or three questions prepared | Ask one | `DEMO.md` | nice |
| **C. Temporal capabilities an SA will probe** | | | | | |
| C1 | Durable execution and crash recovery | One parent Workflow. Completed LLM and tool Activities are not re-executed on replay | Kill the worker mid-Evidence loop, restart, show resume | Temporal UI history; `tests/test_workflow.py::test_replay_is_deterministic`, `::test_worker_restart_resumes_without_rerunning_activities` | must |
| C2 | Retries and timeouts | Per-Activity timeout and `RetryPolicy`. `LLMClient` is built with `max_retries=0` so Temporal owns retries | Self demo `activity_failure_retry`: injected Splunk timeouts and a lost SOAR response; show retry attempts in a drawer and in the Temporal UI | `src/soc/llm.py`, `src/soc/activities.py`; `tests/test_retries.py` | must |
| C3 | Idempotency and exactly-once side effects | Workflow ID `case-{alert_id}`. SOAR Activity idempotency key `soar-submit-{alert_id}`. Mock SOAR unique on `case_id` | Start the same alert twice. Timeout-after-success still yields one ticket | `src/soc/soar.py`; `tests/test_soar_idempotency.py` | must |
| C4 | Signals, Queries, Updates for HITL | Query `get_case`. Update `decide` (analyst + phase). `wait_condition` blocks without a worker thread | Approve/reject in the UI; show the Update in Temporal UI | `src/soc/workflow.py` | must |
| C5 | Timers and escalation | Durable 2-minute demo timer. On timeout: notify, mark `ESCALATED`, keep waiting | Time-skipping test; optional live 2-minute wait | `tests/test_simulations.py::test_simulation[approval_timeout_escalated]` | must |
| C6 | Parallelism (no child Workflows) | `asyncio.gather(EvidenceAgent().run(), ThreatAgent().run())` inside the parent. Same history, easier to read. Threat can be disabled to save tokens | In the Temporal UI, show overlapping Evidence and Threat Activities | `src/soc/workflow.py`, `src/soc/agents.py` | should |
| C7 | Versioning and determinism | No I/O or wall clock in Workflow or Agent.run. `workflow.patched` only if the happy path later changes | Point at `workflow.py` / `agents.py` and explain why the LLM is an Activity | those two files | should |
| C8 | Visibility in the Temporal UI, and in our UI drawers | Every LLM and tool call is an Activity. The case UI stays clean; a "What Temporal did here" drawer shows history, retries, prompt/response, and a link to the Temporal UI | Open the drawer on the triage step, then jump to Cloud UI | UI drawer; namespace `quickstart-vihaggupta.om7xm` | must |
| C9 | Testing with time-skipping | `WorkflowEnvironment.start_time_skipping()` for the approval SLA | `pytest -k approval_timeout` | that test | must |
| C10 | Workers and task queues | One queue, `soc-case`, one worker service. A later `soc-llm` split is a talking point only | Show the single worker container | `src/soc/worker.py`, `compose.yml` | should |
| C11 | Cloud vs local | `ClientConfig`: local compose / `localhost:7233`, or `TEMPORAL_PROFILE=cloud-setup` | Same app, only the profile changes | `README.md` | must |
| **D. Temporal AI pillars** | | | | | |
| D1 | Workflows-as-code across models, tools, systems | One Workflow orchestrates Grok, Splunk tools, notify, and SOAR | Trace `run()` from `fetch_alert` to `soar_submit` | `src/soc/workflow.py` | must |
| D2 | Durable execution for AI | Investigation and the human wait survive worker restarts | Stop the worker after notify, bring it back, approve | C1 + C5 | must |
| D3 | Code the happy path only — no hand-written state machine | Contrast `state.py` + 409s. Status is set only by `_enter()` in `run()`'s happy path and exposed by a Query | Put `state.py` next to `CaseInvestigationWorkflow.run` | `DESIGN.md` §4 | must |
| D4 | HITL approval of agent decisions | DispositionAgent proposes; SOAR runs only after `decide(approve)` | Reject, re-investigate, approve | Update `decide` | must |
| D5 | Self-healing LLM + rate limits | Invalid schema → capped Temporal retries. 429 uses Retry-After. Other 4xx → `NEEDS_REVIEW` | Fixture: two garbled triage answers, then a valid one | `src/soc/llm.py`; `tests/test_agents.py::test_llm_self_heal` | must |
| D6 | Parallel agents | Two Agent subclasses gathered in one Workflow. Scale = more Workflows, not more bots | Start two cases, or one case with both agents | `EvidenceAgent`, `ThreatAgent` | should |
| D7 | Testable AI apps | Stub `LLMClient`; time-skip HITL; assert SOAR once. No API key in CI | `pytest` green on fixtures | `tests/` | must |
| D8 | Observability of LLM inputs/outputs | Activity payloads plus the case-UI drawer (prompt, tools, model requested vs returned) | Open the triage drawer | drawer + Temporal UI | must |
| D9 | Langfuse | Out of the cut and the $50/LOC budget | Mention only if asked | — | nice |
| **E. AI Cookbook recipes** | | | | | |
| E1 | LLM call is an Activity | `LLMClient` used only from the `llm_create` Activity. OpenAI-compatible Chat Completions, `base_url` → xAI, `max_retries=0`. `FixtureLLMClient` when no key. No LiteLLM | Show `llm.py` and one Activity | `src/soc/llm.py` | must |
| E2 | Tool-calling agent, no loop | `TriageAgent` and `DispositionAgent` (subclasses of `Agent`): one LLM Activity, at most one tool, structured result | Triage is 1–3 Activities, not a loop | `src/soc/agents.py` | must |
| E3 | Agentic loop with tool calling | `EvidenceAgent` and `ThreatAgent`: `Agent.run()` holds the `while` loop and history, calls `llm_create` and named tool Activities. Hard cap (default 6). No dynamic plugin registry | Watch Evidence call `search_evidence`, then stop | `src/soc/agents.py`, `src/soc/tools.py` | must |
| E4 | HITL agent | `wait_condition` + 2-minute timer + notify. Update `decide` so bad actors fail synchronously (the recipe uses a Signal) | Approve, reject, timeout | `src/soc/workflow.py` | must |
| E5 | HTTP Retry-After | `LLMClient` maps 429 into the next Temporal retry delay | Fixture a 429; show the delay | `src/soc/llm.py` | should |
| E6 | Structured outputs | Pydantic payloads on triage/disposition. Invalid → D5 | Fixture omits `verdict`, then succeeds | `src/soc/models.py` | should |

## Cuts made for leanness (intentional non-rows)

Child Workflows, a LiteLLM layer, a dynamic tool plugin registry, a rebuilt Case API / MCP / nginx console, Kubernetes, and Langfuse are out of the submission. Parallelism is `asyncio.gather` in the parent. Tools are an explicit list of classes in one file.

## How to use the IDs

Cite them in contracts, PR descriptions, and `DEMO.md`. If a feature has no must-row, it is outside the cut unless a should-row is pulled forward on purpose.

## v2: SOC console slice (DESIGN-v2.md)

| ID | Primitive | Where in code | Test (time-skipping) | Demo moment |
| --- | --- | --- | --- | --- |
| V1 | Queue intake as a long-running Workflow; child Workflow per alert, `ParentClosePolicy.ABANDON` | `src/soc/intake.py` `IntakeWorkflow._open_case` | `test_v2.py::test_intake_round_robin_duplicate_and_queue_outage` | Runbook step 1 |
| V2 | Duplicate alert = no-op via Workflow-id uniqueness (`REJECT_DUPLICATE`, `WorkflowAlreadyStartedError`) | `intake.py` | same test (`duplicates == 1`) | Intake: *Duplicates ignored* |
| V3 | Queue outage: unlimited-retry Activity, producers keep enqueueing, drained on recovery | `activities.receive_alerts`, `services/mock_queue.py` (`always_up`) | same test (`attempt > 1`, queue empty) | Chaos *Queue down* |
| V4 | Continue-as-new carrying state (round-robin pointer, counters) | `IntakeWorkflow.run` | `test_intake_continues_as_new_and_keeps_counters` | Temporal UI: `alert-intake` runs |
| V5 | Roster and round-robin auto-assign | `models.ROSTER`, `intake.py` | intake test (`vihag, aisha, ben`) | Runbook step 2 |
| V6 | `reassign` Update + validator; the new analyst continues at the same step | `workflow.py` `reassign` | `test_reassign_moves_the_case_to_the_new_analyst`, `test_api.py::test_actions_go_through_update_validators` | Step 5 |
| V7 | Park/snooze: durable timer (`timeout_summary="park timer"`), SLA paused, auto-resume or `unpark` Update | `workflow.py` `_stay_parked`, `park`, `unpark` | `test_park_timer_resumes_and_sla_does_not_run_while_parked` | Step 6 |
| V8 | Ask-an-agent: Update → child `AskAgentWorkflow` → agent loop with tools incl. `lookup_exemptions` | `workflow.py` `ask`, `AskAgentWorkflow`; `agents.InvestigatorAgent`; `tools.LookupExemptions` | `test_ask_agent_runs_as_child_and_attaches_findings` | Step 7 |
| V9 | Model outage heals by retry (503, 429 with Retry-After → `next_retry_delay`) | `llm.GrokClient`, `activities.OPTIONS` | `test_model_outage_heals`, `test_agents.py` | Chaos *Grok 503/429* |
| V10 | Worker crash and replay | `worker.WorkerSupervisor.down_for` | `test_workflow.py` restart test | Chaos *Kill worker* |
| V11 | `wait_condition(all_handlers_finished)` before completing (no lost Update results) | `CaseInvestigationWorkflow.run` | ask test (approve while the child runs) | — |
| V12 | Chaos panel (UI + CLI) via the mocks' `/admin/outage` switch | `src/soc/chaos.py`, `services/common.py`, `api.chaos` | `test_model_outage_heals` (`fail_next`) | Chaos bar |
