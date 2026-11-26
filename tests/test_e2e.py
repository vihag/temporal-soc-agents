"""End to end: the presenter runbook (demo/runbook.yaml) played by the demo director against the
real console API, an in-process worker and a real local Temporal dev server (wall-clock time,
because chaos outages are measured in real seconds). About a minute."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import uvicorn
from temporalio.testing import WorkflowEnvironment

from soc import api
from soc.director import Director
from soc.intake import ensure_intake
from soc.settings import Settings
from soc.worker import WorkerSupervisor

ROOT = Path(__file__).parents[1]
LOCAL_CLI = Path.home() / ".temporalio/bin/temporal"  # reuse it if installed; CI downloads


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_presenter_runbook_end_to_end(settings):
    existing = str(LOCAL_CLI) if LOCAL_CLI.exists() else None
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=existing) as env:
        task_queue = Settings.from_env().task_queue
        api._client, api._supervisor = env.client, WorkerSupervisor(env.client, task_queue)
        api._supervisor.start()
        await ensure_intake(env.client, task_queue)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(api.app, port=port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        try:
            director = Director(f"http://127.0.0.1:{port}", fast=True, prefix="E2E")
            await director.run(Director.load(ROOT / "demo" / "runbook.yaml"))
        finally:
            server.should_exit = True
            await serving
            await api._supervisor.stop()
            api._client = api._supervisor = None
    assert os.environ["GROK_CLIENT"] == "fixture"  # the whole run used Mock Grok
