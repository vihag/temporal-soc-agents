"""Mock SOAR's half of exactly-once: unique on case_id."""

from services.mock_soar import SoarStore


def test_resubmit_returns_same_ticket():
    store = SoarStore()
    code, first = store.submit("case-X", "isolate_host", "vihag")
    again_code, again = store.submit("case-X", "isolate_host", "vihag")
    assert (code, again_code) == (201, 200)
    assert again["soar_id"] == first["soar_id"] and again["created"] is False
    assert len(store.tickets) == 1


def test_conflicting_action_is_409():
    store = SoarStore()
    store.submit("case-X", "isolate_host", "vihag")
    assert store.submit("case-X", "close", "vihag")[0] == 409
