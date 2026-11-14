import pytest

from scripts.gates.claims import refusal
from scripts.swarm import dispatcher
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store  # noqa: F401


class CommentingLedger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.comments = []

    def state(self, slug):
        return {**super().state(slug), "tasks": [dict(row) for row in self.rows.values()]}

    def update_task(self, slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        assert (slug, by) == ("sw", "swarm")
        return super().update_task(slug, task_id, fields, by, if_state, if_match)

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


def lives(store, n, task="t1"):  # noqa: F811
    for _ in range(n):
        store.count_claim("sw", task)


@pytest.mark.feature("swarm-tick-launch-outcomes-legacy")
def test_failed_launches_leave_the_task_open_without_spending_lives(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime(fail=True)
    for now_ms in (1_000, 2_000, 3_000, 4_000):
        tick("sw", store, ledger, runtime, now_ms=now_ms)
    comments = [row for row in ledger.comments if row[-1] != dispatcher.AUTHOR]
    assert (store.claims("sw", "t1"), ledger.rows["t1"]["state"], comments) == (0, "open", [])
    assert store.launch_failure("sw", "t1") == "herdr down"


@pytest.mark.feature("swarm-tick-launch-outcomes-legacy")
def test_without_a_pending_handoff_the_summary_says_none(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 4)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.comments == [("sw", "t1", refusal(4, "none", "none", "sw", "t1"), "swarm")]
    assert "drained" in actions
    assert ledger.notes == ["The swarm has no task left to start, one blocked task waits for you"]
