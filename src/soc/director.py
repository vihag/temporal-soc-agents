"""Demo director: plays a declarative runbook (demo/runbook.yaml) against the console's HTTP API.

Every step uses a hook the console already exposes (seed, alerts, chaos, analyst Updates, case
status, event history), so the director knows nothing about Temporal internals. The same
runbook is the end-to-end test: `soc demo run --fast` skips the presenter pauses.

    steps:
      - caption: {text: "Alerts land on a queue", pause: 5}
      - seed: {}
      - wait_until: {case: "1001", status: DISPOSITION_PROPOSED}
      - act: {case: "1006", action: reassign, to: "@next"}
      - assert_history: {case: "2001", kind: activity, label: llm_create, min_attempt: 2}
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml


class RunbookFailed(AssertionError):
    """A wait timed out or an assertion did not hold."""


def dig(obj: Any, path: str) -> list[Any]:
    """Values at a dotted path; `list[]` fans out: dig(case, "evidence[].source")."""
    values = [obj]
    for part in path.split("."):
        key, many = part.removesuffix("[]"), part.endswith("[]")
        found = [v.get(key) if isinstance(v, dict) else None for v in values]
        values = [x for f in found for x in (f or [])] if many else found
    return values


def holds(values: list[Any], spec: dict[str, Any]) -> bool:
    """One matcher per spec: equals | contains | at_least | truthy (the default)."""
    if "equals" in spec:
        return bool(values) and all(v == spec["equals"] for v in values)
    if "contains" in spec:
        return spec["contains"] in values
    if "at_least" in spec:
        return any(isinstance(v, int | float) and v >= spec["at_least"] for v in values)
    return any(values)


@dataclass
class Director:
    api: str = "http://127.0.0.1:8080"
    fast: bool = False
    prefix: str = field(default_factory=lambda: f"DEMO-{datetime.now():%H%M%S}")
    say: Callable[[str], None] = print
    http: httpx.AsyncClient = field(init=False)
    steps: list[Any] = field(init=False, default_factory=list)

    async def run(self, runbook: dict[str, Any]) -> None:
        steps = self.steps = runbook["steps"]
        async with httpx.AsyncClient(base_url=self.api, timeout=60) as self.http:
            self.roster = (await self.http.get("/api/meta")).json()["roster"]
            for n, step in enumerate(steps, 1):
                ((kind, args),) = step.items()
                self.say(f"[{n}/{len(steps)}] {kind} {args or ''}")
                await getattr(self, kind)(args or {}, n)
            await self.http.post("/api/caption", json={"text": ""})
        self.say(f"Runbook '{runbook.get('name', '')}' passed (cases case-{self.prefix}-*).")

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        return yaml.safe_load(Path(path).read_text())

    def case_id(self, suffix: str) -> str:
        return f"case-{self.prefix}-{suffix}"

    async def _pause(self, seconds: float) -> None:
        if not self.fast:
            await asyncio.sleep(seconds)

    # ---- steps: one method per runbook verb ----

    async def caption(self, a: dict[str, Any], n: int) -> None:
        await self.http.post(
            "/api/caption", json={"text": a["text"], "step": n, "total": len(self.steps)}
        )
        await self._pause(a.get("pause", 5))

    async def pause(self, a: dict[str, Any], n: int) -> None:
        await self._pause(a.get("seconds", 3))

    async def seed(self, a: dict[str, Any], n: int) -> None:
        (await self.http.post("/api/seed", json={"prefix": self.prefix})).raise_for_status()

    async def alert(self, a: dict[str, Any], n: int) -> None:
        body = {"alert_id": f"{self.prefix}-{a['id']}", "scenario": a["scenario"]}
        body["approval_timeout_s"] = a.get("approval_timeout_s", 120)
        (await self.http.post("/api/alerts", json=body)).raise_for_status()

    async def chaos(self, a: dict[str, Any], n: int) -> None:
        response = await self.http.post("/api/chaos", json=a)
        if response.status_code == 409 and a.get("optional", True):
            self.say(f"  skipped: {response.json()['detail']}")  # e.g. worker in another process
            return
        response.raise_for_status()

    async def wait_until(self, a: dict[str, Any], n: int) -> None:
        cid, loop = self.case_id(a["case"]), asyncio.get_running_loop()
        deadline = loop.time() + a.get("timeout", 90)
        want = a.get("status")
        statuses = [want] if isinstance(want, str) else want
        while loop.time() < deadline:
            response = await self.http.get(f"/api/cases/{cid}")
            if response.status_code == 200:  # the case may not exist yet: keep polling
                case = response.json()
                if (case["status"] in statuses) if statuses else holds(dig(case, a["path"]), a):
                    return
            await asyncio.sleep(0.5)
        raise RunbookFailed(f"step {n}: {cid} never reached {want or a}")

    async def wait_list(self, a: dict[str, Any], n: int) -> None:
        """The case shows up in the queue filtered by status/assignee (a Visibility query)."""
        cid = self.case_id(a["case"])
        params: dict[str, str] = {k: str(a[k]) for k in ("status", "assignee") if k in a}
        for _ in range(int(a.get("timeout", 30) * 2)):
            listing = (await self.http.get("/api/cases", params=params)).json()
            if any(row["id"] == cid for row in listing["rows"]):
                return
            await asyncio.sleep(0.5)
        raise RunbookFailed(f"step {n}: {cid} not in the queue for {params}")

    async def act(self, a: dict[str, Any], n: int) -> None:
        cid = self.case_id(a["case"])
        case = (await self.http.get(f"/api/cases/{cid}")).json()
        body = {k: v for k, v in a.items() if k not in ("case", "action", "as", "to", "expect")}
        body["analyst_id"] = self.analyst(a.get("as", "@assignee"), case["assignee"])
        if "to" in a:
            body["to_analyst"] = self.analyst(a["to"], case["assignee"])
        response = await self.http.post(f"/api/cases/{cid}/{a['action']}", json=body)
        refused = response.status_code == 409
        if refused != (a.get("expect") == "refused"):
            raise RunbookFailed(f"step {n}: {a['action']} on {cid} -> {response.text}")
        if refused:
            self.say(f"  refused as expected: {response.json()['detail']}")

    def analyst(self, who: str, assignee: str) -> str:
        """@assignee, @other (anyone else) or @next (the next analyst on the roster)."""
        others = [r for r in self.roster if r != assignee]
        nxt = self.roster[(self.roster.index(assignee) + 1) % len(self.roster)]
        return {"@assignee": assignee, "@other": others[0], "@next": nxt}.get(who, who)

    async def assert_case(self, a: dict[str, Any], n: int) -> None:
        case = (await self.http.get(f"/api/cases/{self.case_id(a['case'])}")).json()
        if not holds(dig(case, a["path"]), a):
            raise RunbookFailed(f"step {n}: {a['path']} = {dig(case, a['path'])}, wanted {a}")

    async def assert_history(self, a: dict[str, Any], n: int) -> None:
        """Some bar on the case's real event history matches kind/label/min_attempt/fired."""
        bars = (await self.http.get(f"/api/cases/{self.case_id(a['case'])}/history")).json()
        if not any(self._bar_matches(b, a) for b in bars["bars"]):
            raise RunbookFailed(f"step {n}: no history bar like {a} on case {a['case']}")

    @staticmethod
    def _bar_matches(bar: dict[str, Any], a: dict[str, Any]) -> bool:
        return (
            bar["kind"] == a.get("kind", bar["kind"])
            and a.get("label", "") in bar["label"]
            and bar.get("attempt", 1) >= a.get("min_attempt", 1)
            and bar.get("fired", False) >= a.get("fired", False)
        )

    async def run_report(self, a: dict[str, Any], n: int) -> None:
        """Trigger the shift-report Schedule now, wait for that run, check what it counted."""
        before = ((await self.http.get("/api/report")).json()["latest"] or {}).get("at")
        (await self.http.post("/api/report")).raise_for_status()
        for _ in range(int(a.get("timeout", 30) * 2)):
            latest = (await self.http.get("/api/report")).json()["latest"]
            if latest and latest["at"] != before:
                if not holds(dig(latest, a["path"]), a):
                    raise RunbookFailed(f"step {n}: report {a['path']} = {dig(latest, a['path'])}")
                self.say(f"  report: {latest['by_status']}")
                return
            await asyncio.sleep(0.5)
        raise RunbookFailed(f"step {n}: the triggered shift report never finished")

    async def assert_intake(self, a: dict[str, Any], n: int) -> None:
        intake = (await self.http.get("/api/intake")).json()
        if not holds(dig(intake, a["path"]), a):
            raise RunbookFailed(f"step {n}: intake {a['path']} = {dig(intake, a['path'])}")
