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


async def test_actions_go_through_update_validators(env, settings):
    api._client = env.client
    from soc.models import CaseInput, Status
    from soc.workflow import CaseInvestigationWorkflow as CaseWF

    handle = await env.client.start_workflow(
        CaseWF.run,
        CaseInput("hitl-confirm-01", alert_id="SPL-T-API"),
        id="case-SPL-T-API",
        task_queue=settings.task_queue,
    )
    for _ in range(200):
        if (await handle.query(CaseWF.get_case)).status == Status.DISPOSITION_PROPOSED:
            break
        await env.sleep(0.5)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        url = "/api/cases/case-SPL-T-API"
        wrong = await http.post(f"{url}/park", json={"analyst_id": "ben", "seconds": 60})
        assert wrong.status_code == 409 and "assignee" in wrong.json()["detail"]
        parked = await http.post(f"{url}/park", json={"analyst_id": "vihag", "seconds": 60})
        assert parked.json()["status"] == "PARKED"
        moved = await http.post(
            f"{url}/reassign", json={"analyst_id": "vihag", "to_analyst": "aisha"}
        )
        assert moved.json()["assignee"] == "aisha"
        asked = await http.post(f"{url}/ask", json={"analyst_id": "aisha", "question": "logins?"})
        assert asked.json() == {"accepted": True}
        done = await http.post(f"{url}/decide", json={"analyst_id": "aisha", "decision": "approve"})
        assert done.status_code == 200
        bars = (await http.get(f"{url}/history")).json()["bars"]
        labels = [b["label"] for b in bars if b["kind"] == "update"]
        assert labels[:2] == ["park(60s)", "reassign(→ aisha)"]
        assert any(b["kind"] == "timer" and b["label"].startswith("park timer") for b in bars)
    assert (await handle.result()).status == Status.SUBMITTED
