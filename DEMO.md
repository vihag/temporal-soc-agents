# Demo script

About 20 minutes of a 20–30 minute Temporal SA exercise review. The point is how Temporal handles distributed-systems problems, not a tour of SOC features. Deep Temporal detail lives in the case UI's **What Temporal did here** drawers — open them when you want to go down a level, the way the ClickHouse demo opened "SQL + EXPLAIN".

Full talking points and the cost/LOC story sit in [`DESIGN.md`](DESIGN.md). Walk the code with [`CODE_TOUR.md`](CODE_TOUR.md).

## Before the call

- `docker compose up` (or the 3-terminal fallback in the README) is already running.
- Prefer **Temporal Cloud**, profile `cloud-setup`, namespace `quickstart-vihaggupta.om7xm`, so the UI link in the drawer is the same one the interviewer can be talked through. Local is fine as a backup.
- Rehearse once on fixtures (`GROK_CLIENT=fixture`). Optionally one live Grok triage if the key is set. Do not soak the API; see the $50 cap in DESIGN.md §11.
- A second terminal is ready to stop and start the worker (`docker compose restart temporal-worker` or Ctrl-C / rerun).
- Threat agent: leave **on** if you want the parallelism beat; off if you want a shorter history.

## Script

0. **Self demo opener** (2 min, optional). Open `http://localhost:8080/#demo=activity_failure_retry`. On the left, the real `workflow.py`, with the executing line highlighted. On the right, the Gantt built from the real event history: a tool call fails twice, a model answer is garbled, and the SOAR response is lost. Each shows as a red segment, then green. The stepper goes *Workflow runs → Something breaks → It recovers*. Click any bar for its inputs, outputs, error and Activity code.

1. **Problem and form factor** (2 min). A Splunk alert has to become a contain / escalate / close decision, with a human gate and a single SOAR ticket. The old system is a state table, bots, and a webhook. This is one Workflow and a small analyst app. Show `diagrams/architecture.png` and `diagrams/case-states.png` if useful, then the live UI queue.

2. **Start a case** (2 min). From the UI or `python -m soc start --scenario hitl-confirm-01`. Open the case. Point at the Workflow ID `case-…` in the Temporal UI (Cloud if you are on the profile).

3. **Happy path and LLM visibility** (3 min). On the case timeline: triage is 1–3 Activities (tool-calling agent, no loop). Evidence (and Threat, if on) is a short loop of `llm_create` and named tools. Open **one** drawer on the triage step: prompt, response, `model_requested` vs `model` returned, link to the same event in Temporal Cloud.

4. **Kill the worker** (3 min). While Evidence is mid-loop (or start a second case and kill during its loop): `docker compose kill worker`. Show the Workflow still open. `docker compose start worker`. Completed LLM and tool Activities are not re-run. Any call that was in flight when the worker died is retried once its 30s StartToClose timeout passes, and it shows as a red retry segment in the Self demo Gantt and the drawer. This is the durable-execution beat.

5. **Human in the loop** (3 min). Back on the first case (or wait for disposition). The detail page shows the proposal and Approve / Reject — not a wall of history. Approve with a short rationale. Show the Update in Temporal UI and the status move to `SUBMITTED`.

6. **Exactly-once SOAR** (3 min). The case now has one `soar_case_id`. Start the same `alert_id` again: `WorkflowAlreadyStartedError`. Mention (or show the test) that a timeout after SOAR has already written the row still returns that same id.

7. **Timer** (2 min, or pre-run pytest if time is tight). A second case, do not approve. After 2 minutes it is `ESCALATED` and still waiting. A late approve still works. In tests this is `pytest tests/test_approval_timeout.py` with time-skipping, not a live 2-minute sit.

8. **If time: self-heal** (1 min). Point at `tests/test_llm_self_heal.py`, or a fixture run that returns bad JSON twice and then a valid triage.

Stop. Leave at least 10 minutes for their Q&A.

## Short answers to keep ready

- **Why is the LLM an Activity?** Workflows must be deterministic. A model call is I/O. Replay must not call Grok again.
- **Why `max_retries=0` on the client?** So Temporal's retry policy, not the vendor SDK, owns recovery.
- **Why an Update rather than a Signal for `decide`?** The analyst needs a synchronous yes/no (wrong actor, wrong phase). Signals are fire-and-forget. The cookbook uses a Signal; we upgraded it on purpose.
- **Why no child Workflows?** Readability. Parallelism is `asyncio.gather` in one history. Child Workflows would add IDs and files a reviewer has to chase in 15 minutes.
- **How do you scale?** More Workflows, more workers on `soc-case`. A later `soc-llm` queue is a talking point, not this repo.
- **What did we not build?** Child Workflows, LiteLLM, a tool plugin system, Langfuse, a Case API rebuild.

## Questions to ask them

- How are customers versioning long-lived agent Workflows today (`patched` vs Worker Versioning)?
- Is Update-based HITL the pattern they want in the AI cookbook (the current recipe uses a Signal)?
- For Python agents, are they happier with Chat Completions against OpenAI-compatible providers, or with the Responses-API samples as written?

## If something is down

- No Cloud: same script on the local Temporal UI (`http://localhost:8233`).
- No UI: CLI `start` / `view` / `decide` from the README, and the Temporal UI for history.
- No Grok key: fixtures only; say so up front. The distributed-systems story does not depend on a live model.
