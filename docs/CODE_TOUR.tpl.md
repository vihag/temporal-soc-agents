# Learn Temporal through one SOC case

A beginner's path through this repo: ten short lessons, about 5 minutes each. Each lesson explains one idea in plain words, links the exact lines that do it, gives you something to try, and names the test that proves it. You need Python. You do not need to know Temporal.

> Edit `docs/CODE_TOUR.tpl.md`, not this file, then run `uv run python scripts/build_tour.py` to refresh the line links. CI fails if they drift.

**Setup for the "Try it" steps** (fixture mode: no API key, $0):

```bash
temporal server start-dev                  # terminal 1. Temporal UI on http://localhost:8233
uv run python -m soc api --with-worker     # terminal 2. Console on http://localhost:8080
```

---

## Lesson 1: The big idea

A SOC case takes minutes to days. Along the way it calls flaky things (Splunk, a model, SOAR), waits for a human, and must survive restarts. Usually you would build a state table, a job queue, retry loops, cron jobs and idempotency keys.

With Temporal you write the case as **one ordinary async function**, the **Workflow**. Temporal records every step in an **event history** on the server. If the worker process dies, a new worker re-runs the function from the top. Steps that already finished are not executed again: their results are read back from the history. This is **replay**, and it is why the function can "sleep for two days" or "wait for an analyst" safely.

Two kinds of code follow from that:

- **Workflow code** (`workflow.py`, `intake.py`, `report.py`): decisions only. It must be *deterministic*: same history in, same decisions out. No network, no clock, no randomness.
- **Activity code** (`activities.py`): anything that touches the outside world. Temporal retries Activities and records their results.

A **worker** ({{src/soc/worker.py::class SocWorker}}) is your process: it polls a **task queue** and runs both kinds of code. The Temporal server stores state and hands out tasks. It never runs your code.

*Try it*: press **Seed 8 alerts**, open a case, and look at the right-hand timeline. Every bar is a real history event.

---

## Lesson 2: The Workflow is the state machine

There is no status table. The case's status is just a field the Workflow sets as it moves through its code.

- [`Status`]({{src/soc/models.py::class Status}}) lists the states. [`_enter()`]({{src/soc/workflow.py::def _enter}}) is the only place that changes it.
- Read [`_lifecycle()`]({{src/soc/workflow.py::async def _lifecycle}}) top to bottom: fetch the alert → triage → route (auto-resolve / review / investigate) → propose a disposition → wait for the analyst → submit to SOAR. That is the whole state machine, as code.
- [`run()`]({{src/soc/workflow.py::async def run(self, inp: CaseInput)}}) ends with [`wait_condition(all_handlers_finished)`]({{src/soc/workflow.py::all_handlers_finished}}), so an analyst's in-flight request is never cut off.
- Inputs arrive as [`CaseInput`]({{src/soc/models.py::class CaseInput}}). Settings travel in the input because Workflow code may not read environment variables (they could differ on replay).

The diagram: [`diagrams/case-states.png`](diagrams/case-states.png).

*Proved by*: `tests/test_simulations.py` (five scenarios end to end).

---

## Lesson 3: Activities and retries

An Activity is a normal async function marked `@activity.defn`. The Workflow calls it, and Temporal handles timeouts and retries.

- The Activities: [`fetch_alert`]({{src/soc/activities.py::async def fetch_alert}}), [`llm_create`]({{src/soc/activities.py::async def llm_create}}), [`run_tool`]({{src/soc/activities.py::async def run_tool}}), [`notify`]({{src/soc/activities.py::async def notify}}), [`soar_submit`]({{src/soc/activities.py::async def soar_submit}}).
- [`OPTIONS`]({{src/soc/activities.py::OPTIONS: dict}}): every timeout and retry policy in one table. [`call()`]({{src/soc/activities.py::async def call}}) applies the right row. `0` attempts means "retry until it works" (used for the queue).
- The model client is built with [`max_retries=0`]({{src/soc/llm.py::max_retries=0,}}), so only Temporal retries. A 429 becomes [`next_retry_delay`]({{src/soc/llm.py::next_retry_delay}}) = Retry-After.
- **Self-healing**: `llm_create` validates the model's answer and raises if it is malformed. Temporal retries it, and the next sample is usually fine.

*Try it*: **⚡ Chaos → Grok 503**, then seed again. The triage bar turns striped: attempt 2, 3, 4… Click it to see the error and the backoff. The Workflow code never noticed.

*Proved by*: `test_agents.py::test_llm_self_heal`, `test_v2.py::test_model_outage_heals`, `test_agents.py::test_grok_rate_limit_uses_retry_after`.

---

## Lesson 4: Agents, in parallel, rate limited

An "agent" here is a small class, not a service. [`Agent._converse()`]({{src/soc/agents.py::async def _converse}}) is the loop from Temporal's AI cookbook: ask the model (an Activity); if it asks for a tool, run the tool (an Activity); repeat until it answers. Each call is durable, so a crash mid-loop loses nothing.

- [`TriageAgent`]({{src/soc/agents.py::class TriageAgent}}), [`EvidenceAgent`]({{src/soc/agents.py::class EvidenceAgent}}), [`ThreatAgent`]({{src/soc/agents.py::class ThreatAgent}}), [`DispositionAgent`]({{src/soc/agents.py::class DispositionAgent}}) only declare their instructions and tools.
- Parallelism is plain [`asyncio.gather`]({{src/soc/workflow.py::asyncio.gather}}) in the Workflow: Evidence and Threat run side by side, and their bars overlap on the timeline.
- **Rate limiting**: every model call goes to its own task queue, [`<queue>-llm`]({{src/soc/activities.py::def llm_queue}}). The `-llm` worker sets `max_task_queue_activities_per_second` (`LLM_RATE_PER_S`, default 5). The server enforces it across all workers, so a burst of cases queues up instead of hammering the model.

*Proved by*: `test_agents.py::test_parallel_agents`, `test_agents.py::test_evidence_loop_uses_tools`, `test_versioning.py::test_new_escalations_take_the_patched_branch` (checks the `-llm` queue).

---

## Lesson 5: The human gate: Update, timer, Query

The Workflow proposes a disposition and waits for an analyst.

- **Query** = read the Workflow's state without changing it: [`get_case`]({{src/soc/workflow.py::def get_case}}). The console polls it.
- **Update** = a request that changes the Workflow and returns an answer: [`decide`]({{src/soc/workflow.py::async def decide}}). Its [validator]({{src/soc/workflow.py::def _validate_decide}}) runs first and can refuse ("only the assignee may decide"). A refused request never enters the history.
- **Durable timer**: [`wait_condition(..., timeout=sla)`]({{src/soc/workflow.py::timeout_summary="approval SLA"}}) waits for a decision *or* the approval SLA. The timer lives on the server, so it still fires if every worker is down. On timeout the case becomes `ESCALATED`, the lead is paged, and the case keeps waiting.

*Try it*: approve a case as the wrong analyst (switcher, top right): refused. Then approve as the assignee.

*Proved by*: `test_workflow.py::test_duplicate_case_id_and_decide_validation`, `test_simulations.py` (`approval_timeout_escalated` skips a 2-minute timer in milliseconds).

---

## Lesson 6: Intake: child Workflows and idempotency

Alerts arrive on an SQS-like queue. [`IntakeWorkflow`]({{src/soc/intake.py::class IntakeWorkflow}}) is a Workflow that never ends: poll, open cases, ack, sleep, repeat.

- [`_open_case`]({{src/soc/intake.py::async def _open_case}}) starts one **child Workflow** per alert, with id `case-{alert_id}`. [`ABANDON`]({{src/soc/intake.py::ParentClosePolicy.ABANDON}}) lets cases outlive the intake loop.
- **Idempotency for free**: Temporal allows only one Workflow per id. A duplicate alert hits [`REJECT_DUPLICATE`]({{src/soc/intake.py::REJECT_DUPLICATE}}) and raises [`WorkflowAlreadyStartedError`]({{src/soc/intake.py::except WorkflowAlreadyStartedError}}), which is counted and ignored.
- **Ack after start**: the message is deleted only after its case exists. A crash in between means redelivery, and redelivery is a harmless duplicate.
- **Continue-as-new**: an endless loop would grow an endless history, so every N polls it [restarts itself]({{src/soc/intake.py::continue_as_new}}) with fresh history and the same counters.
- [`ensure_intake`]({{src/soc/intake.py::async def ensure_intake}}) is safe to call from every process (`USE_EXISTING`).

*Try it*: Seed, then open **Alert intake**: *Duplicates ignored = 1*.

*Proved by*: `test_v2.py::test_intake_round_robin_duplicate_and_queue_outage`, `::test_intake_continues_as_new_and_keeps_counters`.

---

## Lesson 7: Park and reassign: more Updates and timers

- [`reassign`]({{src/soc/workflow.py::def reassign}}): an Update that moves the case to another analyst. The new owner continues at exactly the same step, because the step is the Workflow's position in its code.
- [`park`]({{src/soc/workflow.py::async def park}}) → [`_stay_parked()`]({{src/soc/workflow.py::async def _stay_parked}}): a second durable timer (`park timer`). The approval SLA does not run while parked. The case resumes when the timer fires, on [`unpark`]({{src/soc/workflow.py::async def unpark}}), or on a decision.
- [`ask`]({{src/soc/workflow.py::async def ask}}): an Update that runs a child [`AskAgentWorkflow`]({{src/soc/workflow.py::class AskAgentWorkflow}}). That child is an agent loop with tools, such as exemption tickets, and its answer is attached to the case.

*Try it*: park a case for 1 minute and watch the `park timer` bar. Ask "Any exemption tickets for this user this week?".

*Proved by*: `test_v2.py::test_reassign_moves_the_case_to_the_new_analyst`, `::test_park_timer_resumes_and_sla_does_not_run_while_parked`, `::test_ask_agent_runs_as_child_and_attaches_findings`.

---

## Lesson 8: Chaos, durability, replay, determinism

- [`Chaos`]({{src/soc/chaos.py::class Chaos}}) flips outage switches on the mocks. [`WorkerSupervisor`]({{src/soc/worker.py::class WorkerSupervisor}}) stops the worker entirely and starts a fresh one.
- **Durability**: with no worker, nothing is lost. Timers keep ticking on the server, and the case simply pauses. The new worker replays the history and carries on. No Activity runs twice.
- **Determinism**: replay only works if the Workflow makes the same decisions again. So Workflow code uses `workflow.now()` instead of the clock, and keeps all I/O in Activities. The SDK's sandbox catches many mistakes.
- **Exactly-once effects**: Temporal runs an Activity *at least* once (a lost response means a retry). SOAR is [unique on `case_id`]({{services/mock_soar.py::class SoarStore}}), so the retry gets the same ticket back. Retries plus an idempotent receiver give exactly-once effects.

*Try it*: **⚡ Chaos → Kill worker** while a case is investigating.

*Proved by*: `test_workflow.py::test_replay_is_deterministic`, `::test_worker_restart_resumes_without_rerunning_activities`, `test_simulations.py` (`activity_failure_retry`: one SOAR ticket).

---

## Lesson 9: Production features: search attributes, Schedules, versioning, codec

- **Search attributes** ([`visibility.py`]({{src/soc/visibility.py::CASE_STATUS = }})): each case [upserts]({{src/soc/workflow.py::def _index}}) `CaseStatus`, `Assignee`, `Severity`. The case queue is then **one Visibility query** ([`case_query`]({{src/soc/visibility.py::def case_query}}), run by [`list_cases`]({{src/soc/api.py::async def list_cases}})) instead of one Query per case. The console shows the query under the filters. The same query works in the Temporal UI.
- **Schedules** ([`report.py`]({{src/soc/report.py::class ShiftReportWorkflow}})): a shift report every 8 hours. A Schedule is durable cron owned by the server, with [overlap = SKIP]({{src/soc/report.py::ScheduleOverlapPolicy.SKIP}}). *Run now* on the Alert intake tab triggers it.
- **Versioning**: escalation now also pages a backup analyst. Cases already running recorded their history without that step, so the new step sits behind [`workflow.patched`]({{src/soc/workflow.py::workflow.patched(PAGE_BACKUP)}}). New runs record a marker and page. Old histories replay without it. `tests/histories/` holds histories from the previous release, and CI replays them.
- **Payload codec** ([`EncryptionCodec`]({{src/soc/codec.py::class EncryptionCodec}})): with `SOC_CODEC_KEY` set, alerts, prompts and answers are AES-GCM encrypted before they leave the process. The server stores only ciphertext. The console decrypts, and [`/codec`]({{src/soc/api.py::async def codec_endpoint}}) lets the Temporal UI decrypt too.
- **Metrics**: `METRICS_BIND=0.0.0.0:9464` serves SDK metrics for Prometheus ([`_runtime`]({{src/soc/settings.py::def _runtime}})).
- **Cloud**: [`connect()`]({{src/soc/settings.py::async def connect}}) reads Temporal's standard env config, so Cloud is configuration only. See [`docs/CLOUD.md`](docs/CLOUD.md) and `make cloud`.
- [`bootstrap()`]({{src/soc/worker.py::async def bootstrap}}) runs the one-time setup (search attributes, intake, schedule). It is idempotent, so any process may run it.

*Proved by*: `test_versioning.py` (both tests), `test_codec.py`, and the e2e runbook (`wait_list` on the PARKED filter, `run_report`).

---

## Lesson 10: The demo, as code

[`Director`]({{src/soc/director.py::class Director}}) plays [`demo/runbook.yaml`](demo/runbook.yaml), a list of steps (`seed`, `chaos`, `act`, `wait_until`, `assert_history`, `run_report`, `caption`…) that call the console's HTTP API, the same one the UI uses. Normal speed shows captions in the console. `--fast` is the end-to-end test in CI (`tests/test_e2e.py`, a real dev server).

```bash
uv run python -m soc demo run          # or: make demo
uv run python -m soc demo run --fast   # or: make demo-fast
```

---

## Where each guarantee lives

| Guarantee | What could go wrong | Where it lives here | What Temporal does for you |
| --- | --- | --- | --- |
| **Durability** | A worker crashes mid-investigation; a 2-day park outlives every deploy | Event history on the server; [`WorkerSupervisor`]({{src/soc/worker.py::class WorkerSupervisor}}) drill | Persists every step and timer. A new worker replays and resumes. No checkpoint code |
| **Consistency** | Two analysts act at once; someone approves a closed case | Update [validators]({{src/soc/workflow.py::def _validate_decide}}); a single writer of status ([`_enter`]({{src/soc/workflow.py::def _enter}})) | One Workflow processes its events one at a time, so there are no races on case state. Validators reject requests before they reach history |
| **Idempotency** | The same alert arrives twice; a queue message is redelivered | Workflow id `case-{alert_id}` + [`REJECT_DUPLICATE`]({{src/soc/intake.py::REJECT_DUPLICATE}}) | Workflow-id uniqueness makes "start" idempotent; no dedupe table |
| **Exactly-once effects** | SOAR's response is lost and the ticket is filed twice | [`soar_submit`]({{src/soc/activities.py::async def soar_submit}}) retries + SOAR unique on `case_id` | At-least-once Activities with durable retries. Pair them with an idempotent receiver |
| **Retries / backpressure** | Grok rate-limits; Splunk times out; the queue is down | [`OPTIONS`]({{src/soc/activities.py::OPTIONS: dict}}), the `-llm` queue rate limit | Server-side retries with backoff (and Retry-After); task-queue rate limits shared across all workers |
| **Timeouts / SLAs** | Nobody approves; a park must end on time | [`wait_condition` + timer]({{src/soc/workflow.py::timeout_summary="approval SLA"}}), park timer | Durable timers that fire even with no worker running |
| **Visibility** | "Show me ben's parked cases" across thousands | [Search attributes]({{src/soc/visibility.py::def case_query}}) + memo | Indexed Visibility store; list queries in one call, also in the Temporal UI |
| **Evolvability** | A deploy changes the Workflow while cases are in flight | [`workflow.patched`]({{src/soc/workflow.py::workflow.patched(PAGE_BACKUP)}}) + replay tests | Patch markers in history; the `Replayer` catches nondeterminism in CI |
| **Confidentiality** | Alert contents and prompts sit in a hosted service | [`EncryptionCodec`]({{src/soc/codec.py::class EncryptionCodec}}) | The codec hook encrypts every payload client-side; the server and Cloud see ciphertext |

What Temporal does **not** do for you: make your side effects idempotent (that is SOAR's `case_id`), or keep Workflow code deterministic (that is your discipline, checked by replay tests).

## The tests (33, no live Grok)

| Test file | What it proves |
| --- | --- |
| `test_simulations.py` | All five scenarios: SLA timer, escalation, reject → re-investigate, retries, one SOAR ticket |
| `test_workflow.py` | Routing, fatal model error → review, duplicate id, validator, replay, worker restart |
| `test_agents.py` | Self-heal, tools over HTTP, parallel agents, 429 / 4xx / tool-call mapping |
| `test_v2.py` | Intake (round robin, duplicate, queue outage, continue-as-new), reassign, park, ask, model outage |
| `test_versioning.py` | Previous-release histories replay; new runs take the patched branch; model calls use the `-llm` queue |
| `test_codec.py` | History holds ciphertext; the console decrypts; codec endpoint round trip |
| `test_api.py`, `test_soar.py` | History → timeline bars; actions through validators; SOAR uniqueness |
| `test_e2e.py` | The whole presenter runbook on a real dev server: search attributes, Schedule, chaos |
| `test_docs.py` | This tour's line links are current |
