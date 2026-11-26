# Presenter runbook

About 20 minutes of a 20–30 minute Temporal SA review. The story is how Temporal handles distributed-systems problems in a real-feeling SOC console, not a SOC feature tour. Every bar on the right-hand timeline and every *What Temporal did here* link opens a drawer with the real inputs, outputs, errors and code, so you can go down a level whenever you are asked.

## Before the call (5 min)

```bash
temporal server start-dev                       # or Temporal Cloud via TEMPORAL_PROFILE
uv run python -m soc api --with-worker          # console :8080, worker + mocks in-process
```

- Open http://localhost:8080 and http://localhost:8233 (Temporal UI) side by side.
- Fixture mode (`GROK_CLIENT=fixture`): $0, deterministic. Do not seed yet.
- Know the cast: analysts **vihag**, **aisha**, **ben** (the switcher, top right; no auth).
- Optional opener: the **Self demo** tab (`/#demo=activity_failure_retry`): the real `workflow.py` with the live line highlighted, next to the Gantt.

**Hands-free option**: `uv run python -m soc demo run` plays this whole table from [`demo/runbook.yaml`](demo/runbook.yaml), with a caption banner in the console at each step. Talk over it, or pause it with Ctrl-C and drive by hand. `--fast` is the same run as an end-to-end check.

## The run (≈ 20 min)

| # | Do | Say / point at |
|---|----|----|
| 1 | **Seed**: press *Seed 8 alerts* (or `python -m soc seed`). Open **Alert intake**. | Alerts land on an SQS-like queue. `IntakeWorkflow` polls it and starts **one child Workflow per alert, id `case-{alert_id}`**. The 8th alert repeats the 1st: *Duplicates ignored = 1*. Temporal refused a second Workflow with the same id; that is the no-op. Round robin: vihag → aisha → ben. |
| 2 | **Assign**: back to **Cases**, *All cases*, then sign in as **ben**, *My cases*. | Assignment is Workflow state, set on intake. Filters are by assignee and status. Benign alerts auto-resolved; real ones wait at *DISPOSITION_PROPOSED*. |
| 3 | **Chaos at step X**: open **⚡ Chaos**, press *Grok 503* (20s), then press *Seed* again or *Alert with 20s approval SLA*. Watch the new case. | Triage `llm_create` turns red-striped: *attempt 2, 3, 4…*. Click the bar: the error, the backoff from `activities.OPTIONS`. The Workflow code never saw the failure. Repeat with *Splunk down*, *Grok 429* (honours Retry-After), *Queue down* (the depth climbs while intake's `receive_alerts` retries; nothing is lost, it drains on recovery). |
| 4 | **Kill the worker**: *Kill worker* (15s) while a case is investigating. | No worker at all, and the case simply pauses. The new worker replays history and continues at the same step. No Activity re-runs. With Compose: `docker compose kill worker` / `start worker`. |
| 5 | **Reassign**: as ben, on `SPL-2026-1006` (USB exfil), *Reassign → aisha*. Switch to aisha. | A validated **Update**. The validator refuses the wrong analyst or a closed case, and refusals never reach history. Aisha sees the full timeline and continues at the same step. Try *Approve* as ben: refused, "only the assignee". |
| 6 | **Park**: as ben, on `SPL-2026-1003` (impossible travel), *Park 1 min*. | A **durable timer** (`park timer` on the Gantt). The approval SLA does not run while parked. It comes back by itself when the timer fires, or with *Unpark now*. Worker restarts do not matter; the timer lives on the server. |
| 7 | **Ask an agent**: on `SPL-2026-1003`, ask *"Any exemption tickets for this user this week?"* | The Update starts a **child Workflow** `AskAgentWorkflow` (blue bar). It runs an agent loop with tools (`lookup_exemptions`, `search_logins`, …), each an Activity. The answer, evidence and findings attach to the case timeline. |
| 8 | **Approve**: as vihag on `SPL-2026-1001`, *Approve*. | Update → `soar_submit`. |
| 9 | **Exactly-once SOAR**: open `SPL-2026-1004` (hitl-flaky): approve it as vihag. | Its SOAR response is lost once. `soar_submit` retries, and SOAR is unique on `case_id`, so the card says *already filed (the retry was harmless)*. One ticket. Click the bar: attempt 2, `created: false`. |
| 10 | **SLA breach**: the *20s approval SLA* alert from step 3. | The `approval SLA` timer fired → *ESCALATED*, the lead paged. It still waits for the analyst. |
| 11 | Close in the **Temporal UI**: open any case or `alert-intake`. | The same history. `alert-intake` shows continue-as-new every 100 polls, counters carried over. |

## Q&A crib

- **Why a Workflow for intake?** The loop is durable: an outage is just a retrying Activity, duplicates are rejected by Workflow-id uniqueness, and continue-as-new keeps history small.
- **Update vs Signal?** Update gives the analyst a synchronous answer and a validator that rejects bad requests before they reach history.
- **Child vs Activity for ask?** The agent loop makes many model and tool calls. As a child it gets its own history and retries, and it is visible as a unit.
- **Determinism?** All I/O is in Activities. Timers use `workflow.now()`. Replay is tested (`test_replay_is_deterministic`).
- **What is still missing for a complete Temporal solution?** See DESIGN-v2.md §8.
