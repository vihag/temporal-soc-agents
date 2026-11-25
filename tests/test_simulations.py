"""Every demo simulation, end to end, on the time-skipping server. A 2-minute approval timer
takes milliseconds here."""

from __future__ import annotations

import pytest

from soc.models import Status
from soc.simulate import SIMULATIONS, Narrator, run_simulation


@pytest.mark.parametrize("name", list(SIMULATIONS))
async def test_simulation(name, env, settings, soar):
    sim = SIMULATIONS[name]
    log: list[str] = []
    timed = (
        sim
        if name != "approval_timeout_escalated"
        else sim.__class__(
            **{**sim.__dict__, "approval_timeout_s": 120}  # the real 2-minute timer, skipped
        )
    )
    case = await run_simulation(env.client, timed, settings, env.sleep, Narrator(log.append))

    assert case.status == sim.expect
    statuses = [e.status for e in case.timeline]
    if name == "auto_resolve":
        assert case.disposition is None and case.soar is None
    if name == "reject_then_reinvestigate":
        assert [d.decision for d in case.decisions] == ["reject", "approve"]
        assert statuses.count(Status.INVESTIGATING) == 2
    if name == "approval_timeout_escalated":
        assert Status.ESCALATED in statuses and any("timer fired" in line for line in log)
    if name == "activity_failure_retry":
        assert case.soar and case.soar.created is False  # the retry found the first ticket
        assert any("retried run_tool" in line for line in log)
        assert any("retried soar_submit" in line for line in log)
        assert any("retried llm_create" in line for line in log)  # self-healed model answer
    if case.soar:
        assert [t["case_id"] for t in soar.store.tickets.values()].count(case.case_id) == 1
