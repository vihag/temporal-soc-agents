"""Environment settings, read once per process (worker, CLI). Never imported by Workflow code paths
that run during replay, because env vars are not deterministic."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.envconfig import ClientConfigProfile
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from soc.codec import data_converter
from soc.models import CaseInput

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
    llm_rate_per_s: float = 5.0  # model calls per second, across all workers (server-enforced)
    search_attributes: bool = True  # index cases (CaseStatus/Assignee/Severity) for Visibility

    @classmethod
    def from_env(cls) -> Settings:
        """Each field reads the upper-case env var of the same name (TASK_QUEUE, XAI_API_KEY...)."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            if (raw := os.environ.get(f.name.upper())) is not None:
                kind: type = type(f.default)
                values[f.name] = raw == "1" if kind is bool else kind(raw)
        return cls(**values)

    def case_input(self, scenario: str) -> CaseInput:
        """The Workflow input for a new case: runtime knobs ride in the input, never env vars."""
        return CaseInput(
            scenario,
            self.enable_threat,
            self.approval_timeout_s,
            self.max_agent_steps,
            indexed=self.search_attributes,
        )


async def connect() -> Client:
    """Connect using the standard Temporal env config (TEMPORAL_ADDRESS, TEMPORAL_PROFILE, ...).

    With nothing set this is the local dev server on localhost:7233; with a cloud profile it is
    Temporal Cloud. Same code either way.
    """
    config = ClientConfigProfile.load().to_client_connect_config()
    config.setdefault("target_host", "localhost:7233")
    return await Client.connect(**config, data_converter=data_converter(), runtime=_runtime())


_RUNTIME: Runtime | None = None


def _runtime() -> Runtime:
    """SDK metrics (workflow/activity latency, task queue lag, ...) as a Prometheus endpoint
    when METRICS_BIND is set, e.g. METRICS_BIND=0.0.0.0:9464. One Runtime per process."""
    global _RUNTIME
    if _RUNTIME is None:
        bind = os.environ.get("METRICS_BIND", "")
        prometheus = TelemetryConfig(metrics=PrometheusConfig(bind_address=bind))
        _RUNTIME = Runtime(telemetry=prometheus) if bind else Runtime.default()
    return _RUNTIME
