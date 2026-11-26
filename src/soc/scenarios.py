"""Recorded alert scenarios (fixtures/scenarios/*.json). They stand in for Splunk, the tools and
the model, so the whole system runs offline, for free, and deterministically."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Override wins; nested dicts are merged one level deep."""
    merged = dict(base)
    for key, value in override.items():
        both_dicts = isinstance(value, dict) and isinstance(base.get(key), dict)
        merged[key] = {**base[key], **value} if both_dicts else value
    return merged


@dataclass
class Scenario:
    name: str
    data: dict[str, Any]

    @classmethod
    def load(cls, name: str, fixtures_dir: Path) -> Scenario:
        """Load `<name>.json` on top of `_base.json` (shared tools and ask-agent answers).

        `"extends": "<other>"` reuses another scenario and overrides its keys, so a variant
        (a timeout, a flaky tool) is a few lines instead of a copy."""
        data = json.loads((fixtures_dir / f"{name}.json").read_text())
        if base := data.pop("extends", None):
            data = _merge(cls.load(base, fixtures_dir).data, data)
        elif name != "_base":
            data = _merge(cls.load("_base", fixtures_dir).data, data)
        return cls(name, data)

    @property
    def alert(self) -> dict[str, Any]:
        return dict(self.data["alert"])

    def model_turn(self, agent: str, step: int) -> dict[str, Any]:
        """The recorded model reply for this agent's `step`-th turn (last one repeats)."""
        script = self.data["llm"][agent]
        return dict(script[min(step, len(script) - 1)])

    def tool_result(self, tool: str) -> dict[str, Any] | None:
        result = self.data.get("tools", {}).get(tool)
        return self.render(result) if result is not None else None

    def render(self, value: Any) -> Any:
        """Fill {host} / {user} / {alert_id} placeholders from this scenario's alert."""
        text = json.dumps(value)
        for key in ("host", "user", "alert_id"):
            text = text.replace("{" + key + "}", str(self.data["alert"].get(key, "")))
        return json.loads(text)

    def fault(self, activity: str) -> int | str:
        """How many attempts of `activity` should fail (or "fatal"). 0 = healthy."""
        return self.data.get("faults", {}).get(activity, 0)
