"""One time-skipping Temporal test server, one mock SOAR and one worker for the whole session."""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from services.mock_soar import serve_in_thread
from soc.settings import Settings
from soc.worker import build_worker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def soar():
    server = serve_in_thread(_free_port())
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def settings(soar) -> Settings:
    mp = pytest.MonkeyPatch()
    mp.setenv("SOAR_URL", f"http://127.0.0.1:{soar.server_port}")
    mp.setenv("GROK_CLIENT", "fixture")  # never a live model in tests
    yield Settings(
        task_queue=f"test-{uuid.uuid4()}", soar_url=f"http://127.0.0.1:{soar.server_port}"
    )
    mp.undo()


@pytest_asyncio.fixture(scope="session")
async def env(settings) -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as env:  # noqa: SIM117
        async with build_worker(env.client, settings.task_queue):  # needs env.client
            yield env
