"""Recorded alert scenarios (fixtures/scenarios/*.json). They stand in for Splunk, the tools and
the model, so the whole system runs offline, for free, and deterministically."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Scenario:
    name: str
    data: dict[str, Any]

    @classmethod
    def load(cls, name: str, fixtures_dir: Path) -> Scenario:
        """Load `<name>.json`. `"extends": "<other>"` reuses another file and overrides its keys,
        so a variant (a timeout, a flaky tool) is a few lines instead of a copy."""
        data = json.loads((fixtures_dir / f"{name}.json").read_text())
        if base := data.pop("extends", None):
            merged = cls.load(base, fixtures_dir).data
            for key, value in data.items():
                is_dict = isinstance(value, dict) and isinstance(merged.get(key), dict)
                merged[key] = {**merged[key], **value} if is_dict else value
            data = merged
        return cls(name, data)

    @property
    def alert(self) -> dict[str, Any]:
        return dict(self.data["alert"])

    def model_turn(self, agent: str, step: int) -> dict[str, Any]:
        """The recorded model reply for this agent's `step`-th turn (last one repeats)."""
        script = self.data["llm"][agent]
        return dict(script[min(step, len(script) - 1)])

    def tool_result(self, tool: str) -> dict[str, Any]:
        return dict(self.data["tools"][tool])

    def fault(self, activity: str) -> int | str:
        """How many attempts of `activity` should fail (or "fatal"). 0 = healthy."""
        return self.data.get("faults", {}).get(activity, 0)
