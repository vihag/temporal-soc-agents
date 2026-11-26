"""Environment settings, read once per process (worker, CLI). Never imported by Workflow code paths
that run during replay, because env vars are not deterministic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from temporalio.client import Client
from temporalio.envconfig import ClientConfigProfile

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    task_queue: str = "soc-case"
    grok_client: str = "fixture"  # "fixture" (default, free) or "http" (live Grok + XAI_API_KEY)
    enable_threat: bool = True
    approval_timeout_s: int = 120
    max_agent_steps: int = 6
    soar_url: str = "http://127.0.0.1:8002"
    splunk_url: str = "http://127.0.0.1:8001"
    mock_grok_url: str = "http://127.0.0.1:8003/v1"
    queue_url: str = "http://127.0.0.1:8004"
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-4.3"
    llm_max_tokens: int = 800
    fixture_latency_s: float = 0.0  # Mock Grok "thinking" time, so the UI demo is watchable
    fixtures_dir: Path = REPO_ROOT / "fixtures" / "scenarios"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            task_queue=env("TASK_QUEUE", cls.task_queue),
            grok_client=env("GROK_CLIENT", cls.grok_client),
            enable_threat=env("ENABLE_THREAT", "1") == "1",
            approval_timeout_s=int(env("APPROVAL_TIMEOUT_S", str(cls.approval_timeout_s))),
            max_agent_steps=int(env("MAX_AGENT_STEPS", str(cls.max_agent_steps))),
            soar_url=env("SOAR_URL", cls.soar_url),
            splunk_url=env("SPLUNK_URL", cls.splunk_url),
            mock_grok_url=env("MOCK_GROK_URL", cls.mock_grok_url),
            queue_url=env("QUEUE_URL", cls.queue_url),
            xai_api_key=env("XAI_API_KEY", ""),
            xai_base_url=env("XAI_BASE_URL", cls.xai_base_url),
            xai_model=env("XAI_MODEL", cls.xai_model),
            llm_max_tokens=int(env("LLM_MAX_TOKENS", str(cls.llm_max_tokens))),
            fixture_latency_s=float(env("FIXTURE_LATENCY_S", "0")),
            fixtures_dir=Path(env("FIXTURES_DIR", str(cls.fixtures_dir))),
        )


async def connect() -> Client:
    """Connect using the standard Temporal env config (TEMPORAL_ADDRESS, TEMPORAL_PROFILE, ...).

    With nothing set this is the local dev server on localhost:7233; with a cloud profile it is
    Temporal Cloud. Same code either way.
    """
    config = ClientConfigProfile.load().to_client_connect_config()
    config.setdefault("target_host", "localhost:7233")
    return await Client.connect(**config)
