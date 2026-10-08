import pytest

from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("seat", ["", "eng-1@sw"])
@pytest.mark.parametrize("state", ["delivered", "read"])
def test_stale_exit_settlement_leaves_transferred_work_with_the_successor(monkeypatch, redis, seat, state):
    inbox = InboxStore(redis)
    item = inbox.send("sender", "sw-eng-1", "contract")
    snapshot = inbox.inbox("sw-eng-1")
    inbox.seats.occupy("eng-1@sw", "sw-eng-2", 1)
    inbox.redirect(item.id, "swarm", "eng-1@sw", "handoff")
    getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "sw-eng-2")
    history = inbox.history(item.id)
    monkeypatch.setattr(inbox, "inbox", lambda address: snapshot)
    exits.settle(inbox, "sw-eng-1", seat, "exited")
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", state)
    assert inbox.history(item.id) == history


@pytest.mark.parametrize("exit_text", ["finished its task and exited", "blocked its task and exited"])
def test_late_message_keeps_the_exit_outcome_after_task_reassignment(redis, exit_text):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    exits.settle(inbox, "sw-eng-1", "", exit_text)
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    item = inbox.send("sender", "sw-eng-1", "late contract")
    rows = {"t1": {"claimed_by": "sw-eng-2", "state": "claimed"}}
    exits.sweep(inbox, "sw", store, lambda: rows)
    assert inbox.get(item.id).state == "cancelled"
    assert exit_text in inbox.get(item.id).reason
    [notice] = inbox.pending_items("sender")
    assert item.id in notice.text


def test_the_exit_notice_to_a_sender_is_informational(redis):
    inbox = InboxStore(redis)
    inbox.send("sender", "sw-eng-1", "contract")
    exits.settle(inbox, "sw-eng-1", "", "finished its task and exited")
    [notice] = inbox.pending_items("sender")
    assert notice.fyi is True


class ClosedAfterTheTickRead(FakeLedger):
    def state(self, slug):
        assert slug == "sw"
        doc = super().state(slug)
        snapshot = {**doc, "tasks": [dict(row) for row in doc["tasks"]]}
        self.rows["t1"]["state"] = "done"
        return snapshot


def test_the_exit_sweep_settles_from_the_live_task_state_not_the_tick_snapshot(redis):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sender", "sw-eng-1", "contract")
    ledger = ClosedAfterTheTickRead([{"id": "t1", "state": "pr", "claimed_by": "sw-eng-1"}])
    tick("sw", store, ledger, FakeRuntime(), 1_000)
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("sw-eng-1", "cancelled")
    assert inbox.mailbox("eng-1@sw") == []
    [notice] = inbox.pending_items("sender")
    assert item.id in notice.text


def test_a_live_closed_task_withdraws_late_mail_even_after_an_exit_to_the_seat_was_recorded(redis):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "exited")
    item = inbox.send("sender", "sw-eng-1", "late contract")
    exits.sweep(inbox, "sw", store, lambda: {"t1": {"claimed_by": "sw-eng-1", "state": "blocked"}})
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("sw-eng-1", "cancelled")
    assert "blocked its task and exited" in inbox.get(item.id).reason


def notice_moved_to_the_seat(inbox, store):
    from scripts.gates.push_stop import TEMPLATE

    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("swarm", "sw-eng-1", TEMPLATE)
    inbox.deliver(item.id, "sw-eng-1")
    inbox.redirect(item.id, "swarm", "eng-1@sw", "sw-eng-1 handed off its seat; moved to eng-1@sw", "sw-eng-1")
    return item


def test_the_sweep_closes_a_push_stop_notice_left_on_a_seat_by_an_agent_that_left(monkeypatch, redis, tmp_path):
    from scripts.swarm.store import AgentRecord

    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = notice_moved_to_the_seat(inbox, store)
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    closed = inbox.get(item.id)
    assert (closed.address, closed.state) == ("eng-1@sw", "done")
    assert (
        closed.reason
        == "done: sw-eng-1 left its seat; its worktree was not found, so whether its branch was pushed is unknown"
    )
    assert [item.id for item in inbox.mailbox("sw-eng-2") if item.state != "done"] == []


def test_the_sweep_closes_a_seat_notice_a_live_successor_already_received(monkeypatch, redis, tmp_path):
    from scripts.swarm.store import AgentRecord

    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = notice_moved_to_the_seat(inbox, store)
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    inbox.redirect(item.id, "swarm", "eng-1@sw", "moved again", "eng-1@sw")
    inbox.deliver(item.id, "sw-eng-2")
    exits.sweep(inbox, "sw", store, dict)
    assert (
        inbox.get(item.id).reason
        == "done: sw-eng-1 left its seat; its worktree was not found, so whether its branch was pushed is unknown"
    )


def test_the_sweep_closes_a_seat_notice_a_live_successor_received_first(monkeypatch, redis, tmp_path):
    from scripts.gates.push_stop import TEMPLATE
    from scripts.swarm.store import AgentRecord

    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("swarm", "sw-eng-1", TEMPLATE)
    inbox.redirect(item.id, "swarm", "eng-1@sw", "sw-eng-1 handed off its seat", "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    inbox.deliver(item.id, "sw-eng-2")
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).reason == (
        "done: sw-eng-1 left its seat; its worktree was not found, so whether its branch was pushed is unknown"
    )


def test_the_sweep_leaves_a_seat_notice_open_while_its_agent_is_live(monkeypatch, redis, tmp_path):
    from scripts.swarm.store import AgentRecord

    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = notice_moved_to_the_seat(inbox, store)
    store.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw"))
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "pending"


def test_the_sweep_names_the_occupant_a_seat_notice_nobody_received_was_sent_to(monkeypatch, redis, tmp_path):
    import subprocess

    from scripts.gates.push_stop import TEMPLATE

    tree = tmp_path / "repo" / "sw-eng-1"
    tree.mkdir(parents=True)
    for args in (("init", "-q", "-b", "sw-eng-1"), ("commit", "-q", "--allow-empty", "-m", "work")):
        subprocess.run(["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@e", *args], check=True)
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-0", 1)
    other = inbox.send("sender", "eng-1@sw", "contract")
    item = inbox.send("swarm", "eng-1@sw", TEMPLATE)
    store.seats.occupy("eng-1@sw", "sw-eng-1", item.created_at)
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).reason == "done: sw-eng-1 left its seat; its branch sw-eng-1 was not pushed"
    assert inbox.get(other.id).state == "pending"


def test_the_sweep_closes_a_seat_notice_sent_before_any_occupant_as_unknown(monkeypatch, redis, tmp_path):
    from scripts.gates.push_stop import TEMPLATE

    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path))
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = inbox.send("swarm", "eng-1@sw", TEMPLATE)
    store.seats.occupy("eng-1@sw", "sw-eng-1", item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).reason == f"done: {exits.UNKNOWN_OWNER}"
