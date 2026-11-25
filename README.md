# temporal-soc-agents

A lean Temporal Python SDK app that runs a SOC case investigation as durable AI agents: triage, an Evidence loop (optional parallel Threat), a disposition, a human approve/reject gate, and an exactly-once SOAR submit.

This README is the quickstart. Then read [CODE_TOUR.md](CODE_TOUR.md) (15-minute walk through the code) and [DEMO.md](DEMO.md) (interview script). The design, diagrams, cost cap, and FORGE plan are in [DESIGN.md](DESIGN.md).

## Run it now (T1: CLI + self-driving simulations, fixture mode, no API key)

```bash
temporal server start-dev                      # terminal 1 (UI on http://localhost:8233)
uv sync                                        # or: pip install -e . pytest pytest-asyncio
uv run python -m soc simulate --all            # terminal 2: five narrated end-to-end scenarios
uv run pytest -q                               # same scenarios on a time-skipping test server
```

`simulate` runs its own worker and Mock SOAR in-process, so `start-dev` is the only other thing that needs to be running. Scenarios: `auto_resolve`, `approve_to_submitted`, `reject_then_reinvestigate`, `approval_timeout_escalated`, `activity_failure_retry`. To run one: `uv run python -m soc simulate activity_failure_retry`.

Drive a case by hand (and rehearse killing the worker mid-case):

```bash
uv run python -m services.mock_soar            # terminal 2
uv run python -m soc worker                    # terminal 3 (Ctrl-C it any time, start it again)
uv run python -m soc start --scenario hitl-confirm-01
uv run python -m soc view case-SPL-2026-0001
uv run python -m soc decide case-SPL-2026-0001 approve --analyst vihag --rationale "Evidence matches the alert"
```

The sections below describe the full app once the UI and Compose land (T3).

## What you need

- Docker, or Python 3.11+ and the [Temporal CLI](https://docs.temporal.io/cli) for the fallback
- Optional: an xAI API key for a live Grok call. **Tests and CI never need one.**

## Quickstart (Compose)

```bash
cp .env.example .env          # leave GROK_CLIENT=fixture unless you have a key
docker compose up --build
```

- Case UI: http://127.0.0.1:8080
- Temporal UI: http://127.0.0.1:8233

Start a collaborative case from the UI, or:

```bash
docker compose exec analyst-api python -m soc start --scenario hitl-confirm-01
```

Open the case, wait for a proposed disposition, Approve or Reject. Click **What Temporal did here** on a timeline step for the Activity history, retries, and LLM I/O, with a link to the Temporal UI.

```bash
docker compose exec analyst-api pytest
```

## Live Grok (optional)

In `.env`:

```bash
GROK_CLIENT=http
XAI_API_KEY=...
XAI_MODEL=grok-4.3
```

Then recreate the worker. Default is `reasoning_effort=none` and a token cap; see DESIGN.md §11 for the $50 spend cap. `make grok-smoke` (once it exists) should exit 2 if the key is unset.

## Temporal Cloud

The same Compose stack can point at Cloud instead of the local Temporal service:

```bash
export TEMPORAL_PROFILE=cloud-setup   # namespace quickstart-vihaggupta.om7xm
# start only worker, analyst-api, mock-splunk, mock-soar
docker compose up --build worker analyst-api mock-splunk mock-soar
```

Connection uses `temporalio.envconfig.ClientConfig`. Do not put API keys in git.

## Without Docker (fallback)

```bash
temporal server start-dev      # terminal 1, UI on :8233
uv sync
uv run python -m soc worker    # terminal 2
uv run python -m soc api       # terminal 3, UI on :8080
uv run python -m soc start --scenario hitl-confirm-01
uv run python -m soc view case-SPL-2026-0001
uv run python -m soc decide case-SPL-2026-0001 approve --analyst vihag --rationale "Evidence matches the alert"
```

Mock Splunk and Mock SOAR need to be started the same way the Makefile will document (`python -m services.mock_splunk` and `python -m services.mock_soar`), or use Compose.

## Layout

See DESIGN.md §5 for the file tree and LOC budget. The six files to read first are listed in CODE_TOUR.md.
