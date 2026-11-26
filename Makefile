# Shortcuts for the commands in README.md. Fixture mode by default: Mock Grok, no key, $0.
.PHONY: help dev api seed demo demo-fast worker test lint cloud

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' Makefile | sed 's/:.*##/\t/'

dev:  ## Temporal dev server on :7233, UI on :8233 (terminal 1)
	temporal server start-dev

api:  ## console :8080 + worker + mocks in one process (terminal 2)
	uv run python -m soc api --with-worker

seed:  ## push the 8 demo alerts onto the queue
	uv run python -m soc seed

demo:  ## play demo/runbook.yaml with captions in the console
	uv run python -m soc demo run

demo-fast:  ## the same runbook, no pauses (the e2e check)
	uv run python -m soc demo run --fast

worker:  ## a standalone worker (scale out by running more)
	uv run python -m soc worker

test:  ## the whole suite: time-skipping tests + one e2e on a local dev server
	uv run pytest -q

lint:  ## what CI runs besides the tests
	uv run ruff check . && uv run ruff format --check . && uv run mypy src services

cloud:  ## console + worker against Temporal Cloud (see docs/CLOUD.md; needs your env vars)
	@test -n "$$TEMPORAL_ADDRESS" -a -n "$$TEMPORAL_NAMESPACE" -a -n "$$TEMPORAL_API_KEY" || \
		{ echo "Set TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE and TEMPORAL_API_KEY first (docs/CLOUD.md)"; exit 2; }
	uv run python -m soc api --with-worker
