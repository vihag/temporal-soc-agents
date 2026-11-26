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
from soc.report import ShiftReportWorkflow, ensure_schedule
from soc.settings import Settings, connect
from soc.visibility import ensure_search_attributes
from soc.workflow import AskAgentWorkflow, CaseInvestigationWorkflow

WORKFLOWS = [CaseInvestigationWorkflow, AskAgentWorkflow, IntakeWorkflow, ShiftReportWorkflow]
ACTIVITIES: list[Callable[..., Any]] = [
    activities.fetch_alert,
    activities.run_tool,
    activities.notify,
    activities.soar_submit,
    activities.shift_report,
    activities.receive_alerts,
    activities.ack_alerts,
]


class SocWorker:
    """Two Temporal Workers in one process, started and stopped together.

    * `main` polls `task_queue`: every Workflow plus the fast Activities.
    * `llm` polls `task_queue-llm`: only `llm_create`, at most `llm_rate_per_s` model calls per
      second. That limit is enforced by the Temporal server for the whole task queue, so it
      holds no matter how many worker processes you run.
    """

    def __init__(self, client: Client, task_queue: str, **options: Any) -> None:
        rate = Settings.from_env().llm_rate_per_s
        self.main = Worker(
            client, task_queue=task_queue, workflows=WORKFLOWS, activities=ACTIVITIES, **options
        )
        self.llm = Worker(
            client,
            task_queue=activities.llm_queue(task_queue),
            activities=[activities.llm_create],
            max_task_queue_activities_per_second=rate,
        )

    async def run(self) -> None:
        await asyncio.gather(self.main.run(), self.llm.run())

    async def shutdown(self) -> None:
        await asyncio.gather(self.main.shutdown(), self.llm.shutdown())

    async def __aenter__(self) -> SocWorker:
        await self.main.__aenter__()
        await self.llm.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await asyncio.gather(self.main.__aexit__(*exc), self.llm.__aexit__(*exc))


def build_worker(client: Client, task_queue: str, **options: Any) -> SocWorker:
    return SocWorker(client, task_queue, **options)


class WorkerSupervisor:
    """Owns an in-process worker so the chaos panel can stop it and bring it back."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client, self.task_queue = client, task_queue
        self.worker: SocWorker | None = None
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


async def bootstrap(client: Client, settings: Settings) -> None:
    """One-time namespace setup, safe to repeat: search attributes, the intake loop, the
    shift-report Schedule. Every step is idempotent, so any process may run it at startup."""
    if settings.search_attributes:
        await ensure_search_attributes(client)
    await ensure_intake(client, settings.task_queue, settings.search_attributes)
    await ensure_schedule(client, settings.task_queue)


async def run_worker() -> None:
    settings = Settings.from_env()
    client = await connect()
    await bootstrap(client, settings)
    print(f"Worker polling task queue {settings.task_queue!r} (LLM: {settings.grok_client})")
    await build_worker(client, settings.task_queue).run()


if __name__ == "__main__":
    asyncio.run(run_worker())
