"""The worker process: polls the `soc-case` task queue and runs Workflow + Activity code.

Kill it at any point and start it again: the Workflow resumes from history. Run more than one
and Temporal spreads the work across them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from soc import activities
from soc.settings import Settings, connect
from soc.workflow import CaseInvestigationWorkflow

ACTIVITIES: list[Callable[..., Any]] = [
    activities.fetch_alert,
    activities.llm_create,
    activities.run_tool,
    activities.notify,
    activities.soar_submit,
]


def build_worker(client: Client, task_queue: str) -> Worker:
    return Worker(
        client, task_queue=task_queue, workflows=[CaseInvestigationWorkflow], activities=ACTIVITIES
    )


async def run_worker() -> None:
    settings = Settings.from_env()
    client = await connect()
    print(f"Worker polling task queue {settings.task_queue!r} (LLM: {settings.grok_client})")
    await build_worker(client, settings.task_queue).run()


if __name__ == "__main__":
    asyncio.run(run_worker())
