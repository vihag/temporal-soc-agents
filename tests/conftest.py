"""One time-skipping Temporal test server, both mock services and one worker for the session."""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from services.common import serve_in_thread
from services.mock_grok import grok_server
from services.mock_queue import queue_server
from services.mock_soar import soar_server
from services.mock_splunk import splunk_server
from soc.settings import Settings
from soc.worker import build_worker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def soar():
    server = serve_in_thread(soar_server(_free_port()))
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def splunk():
    server = serve_in_thread(splunk_server(_free_port()))
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def grok():
    server = serve_in_thread(grok_server(_free_port()))
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def queue():
    server = serve_in_thread(queue_server(_free_port()))
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def settings(soar, splunk, grok, queue) -> Settings:
    mp = pytest.MonkeyPatch()
    mp.setenv("SOAR_URL", f"http://127.0.0.1:{soar.server_port}")
    mp.setenv("SPLUNK_URL", f"http://127.0.0.1:{splunk.server_port}")
    mp.setenv("MOCK_GROK_URL", f"http://127.0.0.1:{grok.server_port}/v1")
    mp.setenv("QUEUE_URL", f"http://127.0.0.1:{queue.server_port}")
    mp.setenv("GROK_CLIENT", "fixture")  # never a live model in tests
    mp.delenv("XAI_API_KEY", raising=False)
    yield replace(Settings.from_env(), task_queue=f"test-{uuid.uuid4()}")
    mp.undo()


@pytest_asyncio.fixture(scope="session")
async def env(settings) -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as env:  # noqa: SIM117
        async with build_worker(env.client, settings.task_queue):  # needs env.client
            yield env
