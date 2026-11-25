# temporal-soc-agents

A lean Temporal Python SDK app that runs a SOC case investigation as durable AI agents: triage, an Evidence loop and a parallel Threat loop, a disposition, a human approve/reject gate with escalation, and an exactly-once SOAR submit.

This README is the quickstart. Then read [CODE_TOUR.md](CODE_TOUR.md) (a 15-minute walk through the code) and [DEMO.md](DEMO.md) (the interview script). Design, diagrams, cost cap and build plan are in [DESIGN.md](DESIGN.md).

Everything runs in **fixture mode** by default: recorded model replies, mock Splunk and SOAR, no API key, no spend.

## Quickstart (Docker Compose)

```bash
cp .env.example .env
docker compose up --build
```

- Case console: http://localhost:8080 (the **Cases** tab, and the **Self demo** tab)
- Temporal UI: http://localhost:8233

Self demo straight from a link: http://localhost:8080/#demo=activity_failure_retry. Crash demo: `docker compose kill worker`, then `docker compose start worker`.

## Quickstart (no Docker)

```bash
temporal server start-dev                       # terminal 1, Temporal UI on :8233
uv sync                                         # or: pip install -e . pytest pytest-asyncio
uv run python -m soc api --with-worker          # terminal 2, console on :8080 (worker + mocks in-process)
```

Or run the five narrated scenarios in the terminal, and the tests:

```bash
uv run python -m soc simulate --all             # auto_resolve, approve_to_submitted, reject_then_reinvestigate,
                                                # approval_timeout_escalated, activity_failure_retry
uv run pytest -q                                # same scenarios on a time-skipping test server
```

Drive a case by hand, with each process separate (so you can kill the worker mid-case):

```bash
uv run python -m services.mock_splunk           # :8001
uv run python -m services.mock_soar             # :8002
uv run python -m soc worker                     # Ctrl-C it any time, start it again
uv run python -m soc start --scenario hitl-confirm-01
uv run python -m soc view case-SPL-2026-0001
uv run python -m soc decide case-SPL-2026-0001 approve --analyst vihag --rationale "Evidence matches the alert"
```

## Live Grok (optional)

In `.env`, set `GROK_CLIENT=http` and `XAI_API_KEY=...` (model `grok-4.3`, `reasoning_effort=none`, `LLM_MAX_TOKENS=800`). Then restart the worker. `uv run python -m soc grok-smoke` makes one live call, or exits 2 without a key. Tests and CI never call Grok. The spend cap is in DESIGN.md §11.

## Temporal Cloud

Connection uses Temporal's standard env config, so the same code runs on Cloud. Set `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and `TEMPORAL_API_KEY` (or `TEMPORAL_PROFILE=<profile>` with a `temporal.toml`). Set `TEMPORAL_UI_URL=https://cloud.temporal.io` so the console's links open Cloud. With Compose, run everything except `temporal`:

```bash
docker compose up --build worker analyst-api mock-splunk mock-soar
```

Never commit API keys; `.env` is git-ignored.
