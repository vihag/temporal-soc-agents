"""The worker process: polls the `soc-case` task queue and runs Workflow + Activity code.

Kill it at any point and start it again: every Workflow resumes from history. Run more than one
and Temporal spreads the work across them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from soc import activities
from soc.intake import IntakeWorkflow, ensure_intake
from soc.settings import Settings, connect
from soc.visibility import ensure_search_attributes
from soc.workflow import AskAgentWorkflow, CaseInvestigationWorkflow

WORKFLOWS = [CaseInvestigationWorkflow, AskAgentWorkflow, IntakeWorkflow]
ACTIVITIES: list[Callable[..., Any]] = [
    activities.fetch_alert,
    activities.llm_create,
    activities.run_tool,
    activities.notify,
    activities.soar_submit,
    activities.receive_alerts,
    activities.ack_alerts,
]


def build_worker(client: Client, task_queue: str) -> Worker:
    return Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=ACTIVITIES)


class WorkerSupervisor:
    """Owns an in-process worker so the chaos panel can stop it and bring it back."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client, self.task_queue = client, task_queue
        self.worker: Worker | None = None
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.worker = build_worker(self.client, self.task_queue)
        self.task = asyncio.create_task(self.worker.run())

    async def stop(self) -> None:
        if self.worker:
            await self.worker.shutdown()
            self.worker = None

    async def down_for(self, seconds: float) -> None:
        """Crash drill: no worker at all for `seconds`, then a fresh one replays and continues."""
        await self.stop()
        await asyncio.sleep(seconds)
        self.start()

    @property
    def running(self) -> bool:
        return self.worker is not None


async def run_worker() -> None:
    settings = Settings.from_env()
    client = await connect()
    if settings.search_attributes:
        await ensure_search_attributes(client)
    await ensure_intake(client, settings.task_queue, settings.search_attributes)
    print(f"Worker polling task queue {settings.task_queue!r} (LLM: {settings.grok_client})")
    await build_worker(client, settings.task_queue).run()


if __name__ == "__main__":
    asyncio.run(run_worker())
