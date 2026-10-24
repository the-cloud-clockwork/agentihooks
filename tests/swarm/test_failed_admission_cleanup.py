from scripts.swarm.tick import SpawnError, tick
from scripts.swarm_v2.admission import ADMITTED, RELEASED
from tests.swarm.test_tick import store, tasks, workers  # noqa: F401
from tests.swarm.test_tick_room import ROOM, RacingRuntime


class InvalidPlacement(RacingRuntime):
    def placement_refusal(self, lane, task):
        if self.taken:
            raise SpawnError("unsupported saved launch", "unsupported")
        return ""


def test_validation_failure_during_cleanup_releases_the_original_failed_launch(store):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), InvalidPlacement(store)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert len(actions) == 1
    assert actions[0].startswith("spawn failed for t1")
    assert actions[0].endswith(ROOM)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert store.claimant("sw", "t1") is None
    assert workers(store) == []
    assert [(row["task"], row["state"], row["error"]) for row in store.launches("sw")] == [("t1", "failed", ROOM)]
    assert store.launch_failure("sw", "t1") == ROOM
    assert runtime.admission.pending_execution_admission_total("sw") == {ADMITTED: 1, RELEASED: 1}
