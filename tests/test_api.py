"""The analyst API is a thin Temporal client: history -> Gantt bars, decide -> Update."""

from __future__ import annotations

import httpx

from soc import api
from soc.simulate import SIMULATIONS, Narrator, run_simulation


async def test_history_bars_and_closed_case_decide(env, settings):
    api._client = env.client
    sim = SIMULATIONS["activity_failure_retry"]
    case = await run_simulation(env.client, sim, settings, env.sleep, Narrator(lambda _: None))
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        bars = (await http.get(f"/api/cases/{case.case_id}/history")).json()["bars"]
        assert bars[0]["kind"] == "workflow" and bars[0]["end"]
        soar = next(b for b in bars if b.get("name") == "soar_submit")
        assert soar["attempt"] == 2 and soar["failed_until"] and soar["result"]["created"] is False
        assert any(b["kind"] == "update" for b in bars)
        closed = await http.post(f"/api/cases/{case.case_id}/decide", json={"decision": "approve"})
        assert closed.status_code == 409
        assert "async def run" in (await http.get("/api/source/workflow")).text
