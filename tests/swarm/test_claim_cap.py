import json

import pytest

from scripts.gates.claims import refusal
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


class CommentingLedger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.comments = []

    def state(self, slug):
        return {**super().state(slug), "tasks": [dict(row) for row in self.rows.values()]}

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        assert (slug, by) == ("sw", "swarm")
        return super().update_task(slug, task_id, fields, by, if_state)

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


def gate_rows():
    from scripts.gates.log import gate_log_path

    path = gate_log_path("sw")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def lives(store, n, task="t1"):  # noqa: F811
    for _ in range(n):
        store.count_claim("sw", task)


def test_every_claim_the_tick_makes_counts_one_agent_life(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (len(runtime.spawned), store.claims("sw", "t1"), store.claims("sw", "t2")) == (1, 1, 0)


def test_failed_launches_leave_the_task_open_without_spending_lives(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime(fail=True)
    for now_ms in (1_000, 2_000, 3_000, 4_000):
        tick("sw", store, ledger, runtime, now_ms=now_ms)
    assert (store.claims("sw", "t1"), ledger.rows["t1"]["state"], ledger.comments) == (0, "open", [])
    assert store.launch_failure("sw", "t1") == "herdr down"


def test_a_successful_launch_after_failures_counts_its_claim_once(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime(fail=True)
    for now_ms in (1_000, 2_000):
        tick("sw", store, ledger, runtime, now_ms=now_ms)
    runtime.fail = False
    tick("sw", store, ledger, runtime, now_ms=3_000)
    tick("sw", store, ledger, runtime, now_ms=4_000)
    assert ([task for _, _, task in runtime.spawned], store.claims("sw", "t1")) == (["t1"], 1)
    assert ledger.rows["t1"]["state"] == "claimed"


def test_a_third_life_is_allowed(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 2)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert store.claims("sw", "t1") == 3
    assert gate_rows() == []


def test_a_fourth_claim_is_refused_and_the_task_blocked_for_the_master(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}, {"id": "t2"}]), FakeRuntime()
    lives(store, 3)
    store.put_handoff("sw", "t1", "handoff body", envelope={"reason": "recycle"})
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t2"]
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.claimant("sw", "t1") is None
    assert ledger.comments == [("sw", "t1", refusal(3, "recycle", "none", "sw", "t1"), "swarm")]
    assert f"blocked t1: {refusal(3, 'recycle', 'none', 'sw', 't1')}" in actions
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in gate_rows()] == [
        ("claims", "deny", "swarm", "t1", refusal(3, "recycle", "none", "sw", "t1"))
    ]


def test_blocking_restarts_the_count_so_a_reopen_buys_three_more_lives(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 3)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.claims("sw", "t1") == 0
    ledger.rows["t1"]["state"] = "open"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]


def test_without_a_pending_handoff_the_summary_says_none(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 4)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.comments == [("sw", "t1", refusal(4, "none", "none", "sw", "t1"), "swarm")]
    assert "drained" in actions
    assert ledger.notes == ["The swarm has no task left to start, one blocked task waits for you"]


def test_observe_lets_the_fourth_claim_through_and_logs_the_would_be_deny(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_GATE_CLAIMS", "enforce")
    store.update("sw", gates={"claims": "observe"})
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 3)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert (ledger.rows["t1"]["state"], ledger.comments, store.claims("sw", "t1")) == ("claimed", [], 4)
    assert [(r["kind"], r["reason"]) for r in gate_rows()] == [("observe", refusal(3, "none", "none", "sw", "t1"))]


def test_off_skips_the_cap(store):  # noqa: F811
    store.update("sw", gates={"claims": "off"})
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 3)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert gate_rows() == []


def test_the_tick_ignores_a_mode_in_its_own_environment(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_GATE_CLAIMS", "off")
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    lives(store, 3)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []
    assert ledger.rows["t1"]["state"] == "blocked"


def test_a_crashed_cap_lets_the_claim_through_and_counts_a_fail_open_row(store, monkeypatch):  # noqa: F811
    def broken(slug, task):
        raise ConnectionError("redis blip")

    monkeypatch.setattr(store, "claims", broken)
    ledger, runtime = CommentingLedger([{"id": "t1"}, {"id": "t2"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1", "t2"]
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in gate_rows()] == [
        ("claims", "fail-open", "swarm", "t1", "ConnectionError: redis blip"),
        ("claims", "fail-open", "swarm", "t2", "ConnectionError: redis blip"),
    ]
