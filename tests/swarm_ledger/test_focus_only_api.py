"""The ledger API stores a focus posted with the only flag through the operations route."""

from tests.swarm_ledger.test_api_v1 import authority_live, live, request  # noqa: F401


def test_a_focus_posted_with_only_true_is_stored_over_http(live: dict) -> None:
    operation = {"op": "freeze_set", "id": "focus-only", "verb": "focus", "target": "phases/p1", "only": True}
    status, _ = request(live, "POST", "operations", {"ops": [operation], "guards": {}})
    assert status == 200
    status, reply = request(live, "GET", "freezes")
    assert status == 200
    assert [(row["verb"], row["target"]) for row in reply["data"]] == [("focus", "phases/p1")]
