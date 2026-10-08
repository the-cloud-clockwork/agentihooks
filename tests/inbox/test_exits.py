import pytest

from scripts.inbox import exits
from scripts.inbox.store import CLOSED, InboxStore
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


def test_the_sweep_closes_a_wait_ended_notice_left_on_a_seat_by_an_agent_that_left(redis):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    answered = inbox.send("master@sw", "eng-1@sw", "which branch?")
    inbox.close(answered.id, "sw-eng-1", "done", "answered on the ledger")
    text = "Your wait on checks on https://x/pull/1, now red has ended. Pick task t1 back up: agentihooks swarm sw done"
    notice = inbox.send("swarm", "eng-1@sw", text)
    other = inbox.send("master@sw", "eng-1@sw", "Your wait on the master has ended. Pick task t1 back up: then?")
    inbox.deliver(notice.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", notice.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == ("done", "done: sw-eng-1 left its seat before picking task t1 back up")
    assert inbox.get(other.id).state == "pending"


def test_the_sweep_leaves_a_wait_ended_notice_open_while_its_agent_is_live(redis):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    notice = inbox.send("swarm", "eng-1@sw", "Your wait on task t2, now done has ended. Pick task t1 back up: done")
    store.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw"))
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(notice.id).state == "pending"


def test_the_sweep_closes_a_gone_agents_wait_notice_after_a_live_agents_on_the_same_seat(monkeypatch, redis):
    from scripts.inbox import store as inbox_store
    from scripts.swarm.store import AgentRecord

    clock = iter(range(100, 200))
    monkeypatch.setattr(inbox_store, "now_ms", lambda: next(clock))
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    live = inbox.send("swarm", "eng-1@sw", "Your wait on task t2, now done has ended. Pick task t1 back up: done")
    store.seats.occupy("eng-1@sw", "sw-eng-2", live.created_at + 1)
    gone = inbox.send("swarm", "eng-1@sw", "Your wait on task t3, now done has ended. Pick task t4 back up: done")
    store.seats.occupy("eng-1@sw", "sw-eng-1", gone.created_at + 1)
    store.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw"))
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(live.id).state == "pending"
    assert inbox.get(gone.id).reason == "done: sw-eng-2 left its seat before picking task t4 back up"


def test_exit_sweep_reads_only_unsettled_mail(redis, monkeypatch):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    closed = inbox.send("sender", "sw-eng-1", "finished work")
    inbox.close(closed.id, "sw-eng-1", "done", "finished")
    pending = inbox.send("sender", "sw-eng-1", "work remains")
    read = inbox.send("sender", "sw-eng-1", "read work remains")
    inbox.read(read.id, "sw-eng-1")
    seen = []
    original = inbox.get

    def get(item_id):
        seen.append(item_id)
        return original(item_id)

    monkeypatch.setattr(inbox, "get", get)
    exits.sweep(inbox, "sw", store, lambda: {})
    assert closed.id not in seen
    assert original(pending.id).address == "eng-1@sw"
    assert original(read.id).address == "eng-1@sw"
    assert original(read.id).state == "pending"


def test_live_peer_mail_sweep_does_not_read_closed_history(redis, monkeypatch):
    from scripts.swarm.store import AgentRecord

    store, inbox = RedisStore(redis), InboxStore(redis)
    store.put_agent("sw", AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    closed = inbox.send("sender", "eng-1@sw", "finished", task="old")
    inbox.close(closed.id, "sw-eng-1", "done", "finished")
    live = inbox.send("sender", "eng-1@sw", "work", task="t1")
    inbox.deliver(live.id, "sw-eng-1")
    seen = []
    get = inbox.get

    def read(item_id):
        seen.append(item_id)
        return get(item_id)

    monkeypatch.setattr(inbox, "get", read)
    exits.sweep(inbox, "sw", store, dict)
    assert closed.id not in seen
    assert get(live.id).state == "delivered"


@pytest.mark.parametrize("state", ["delivered", "read", "redirected"])
def test_the_sweep_settles_departed_peer_task_mail_after_seat_reassignment(redis, state):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    departed = AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw")
    peer = AgentRecord(name="sw-eng-3", lane="eng", task="t3", seat="eng-2@sw")
    successor = AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw")
    for agent in (departed, peer):
        store.record_launch("sw", agent, "started")
    store.seats.occupy(departed.seat, departed.name, 1)
    item = inbox.send(peer.name, departed.seat, "Please tell me when your branch is pushed.", task="t1")
    inbox.deliver(item.id, departed.name)
    if state == "read":
        inbox.read(item.id, departed.name)
    elif state == "redirected":
        inbox.redirect(item.id, "swarm", departed.seat, "receiver handed off", departed.seat)
    general = inbox.send("master@sw", departed.seat, "Keep this for the next occupant.")
    store.put_agent("sw", successor)
    store.seats.occupy(successor.seat, successor.name, item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    closed = inbox.get(item.id)
    assert closed.state == "cancelled"
    assert closed.reason == "cancelled: sw-eng-1 left its seat and task t1 before closing it"
    assert inbox.get(general.id).state == "pending"
    assert inbox.history(item.id)[-1]["by"] == "swarm"
    notices = inbox.inbox(peer.name)
    assert len(notices) == 1
    assert notices[0].fyi
    assert notices[0].text == (
        f"sw-eng-1 left its seat and task t1 before closing your message {item.id}: "
        "Please tell me when your branch is pushed.. "
        "It is closed; send it to whoever carries that work on if it still matters."
    )
    exits.sweep(inbox, "sw", store, dict)
    assert len(inbox.inbox(peer.name)) == 1


@pytest.mark.parametrize(
    "retained",
    [
        "same task",
        "live receiver",
        "unknown sender",
        "master sender",
        "information",
        "unreceived",
        "unknown task",
        "empty seat",
        "closed",
    ],
)
def test_the_sweep_keeps_peer_mail_that_still_belongs_to_the_seat(redis, retained):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    departed = AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw")
    peer = AgentRecord(name="sw-eng-3", lane="eng", task="master" if retained == "master sender" else "t3")
    if retained != "unknown task":
        store.record_launch("sw", departed, "started")
    if retained != "unknown sender":
        store.record_launch("sw", peer, "started")
    store.seats.occupy(departed.seat, departed.name, 1)
    item = inbox.send(
        peer.name,
        departed.seat,
        "Keep this message.",
        fyi=retained == "information",
        task="" if retained in ("unknown sender", "master sender", "unknown task") else "t1",
    )
    if retained != "unreceived":
        inbox.deliver(item.id, departed.name)
    if retained == "closed":
        inbox.close(item.id, departed.name, "done", "answered on the ledger")
    before = inbox.get(item.id)
    if retained == "live receiver":
        store.put_agent("sw", departed)
    else:
        successor = AgentRecord(
            name="sw-eng-2",
            lane="eng",
            task="t1" if retained == "same task" else "t2",
            seat="" if retained == "empty seat" else departed.seat,
        )
        store.put_agent("sw", successor)
        store.seats.occupy(departed.seat, successor.name, item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    after = inbox.get(item.id)
    if retained in ("live receiver", "unreceived", "closed"):
        assert after == before
    else:
        assert (after.address, after.state) == (departed.seat, "pending")
    assert inbox.inbox(peer.name) == []


def test_the_sweep_returns_general_peer_mail_a_departed_agent_took_to_its_seat(redis):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    general = inbox.send("sw-eng-3", "eng-1@sw", "Carry this shared seat rule to the next occupant.")
    inbox.deliver(general.id, "sw-eng-1")
    legacy = inbox.send("sw-eng-3", "eng-1@sw", "Legacy seat mail.")
    redis.hdel(inbox.key("item", legacy.id), "task")
    inbox.deliver(legacy.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", general.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(general.id).state == "pending"
    assert inbox.get(legacy.id).task == ""
    assert inbox.get(legacy.id).state == "pending"
    assert inbox.inbox("sw-eng-3") == []


@pytest.mark.parametrize("receiver_state", ["delivered", "read"])
def test_the_sweep_keeps_task_mail_taken_up_by_the_live_successor(redis, receiver_state):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sw-eng-3", "eng-1@sw", "Finish the old work.", task="t1")
    inbox.deliver(item.id, "sw-eng-1")
    inbox.redirect(item.id, "swarm", "eng-1@sw", "handed off", "eng-1@sw")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    if receiver_state == "read":
        inbox.read(item.id, "sw-eng-2")
    else:
        inbox.deliver(item.id, "sw-eng-2")
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == receiver_state
    assert inbox.inbox("sw-eng-3") == []


def test_the_sweep_continues_past_retained_mail_and_finished_or_unseated_agents(redis):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.put_agent("sw", AgentRecord(name="sw-eng-0", lane="eng", task="t0", seat="eng-0@sw", state="finished"))
    store.put_agent("sw", AgentRecord(name="sw-ci-1", lane="ci", task="t0"))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    general = inbox.send("sw-eng-3", "eng-1@sw", "General seat mail.")
    unreceived = inbox.send("sw-eng-3", "eng-1@sw", "New task mail.", task="t1")
    read = inbox.send("sw-eng-3", "eng-1@sw", "Read without delivery.", task="t1")
    inbox.read(read.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", read.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(general.id).state == "pending"
    assert inbox.get(unreceived.id).state == "pending"
    assert inbox.get(read.id).state == "cancelled"
    assert inbox.get(read.id).reason == "cancelled: sw-eng-1 left its seat and task t1 before closing it"


def test_a_message_moved_during_the_sweep_is_kept_and_its_sender_is_not_told(redis, monkeypatch):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sw-eng-3", "eng-1@sw", "Finish the old work.", task="t1")
    inbox.deliver(item.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    withdraw = inbox.withdraw

    def move_then_withdraw(item_id, by, reason, expected_address="", expected_receiver=""):
        inbox.redirect(item_id, "sw-eng-2", "eng-2@sw", "work moved", "eng-1@sw")
        return withdraw(item_id, by, reason, expected_address, expected_receiver)

    monkeypatch.setattr(inbox, "withdraw", move_then_withdraw)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).address == "eng-2@sw"
    assert inbox.get(item.id).state == "pending"
    assert inbox.inbox("sw-eng-3") == []


def test_a_successor_receiving_during_the_sweep_keeps_the_message(redis, monkeypatch):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sw-eng-3", "eng-1@sw", "Finish the old work.", task="t1")
    inbox.deliver(item.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    withdraw = inbox.withdraw

    def receive_then_withdraw(*args, **kwargs):
        inbox.read(item.id, "sw-eng-2")
        return withdraw(*args, **kwargs)

    monkeypatch.setattr(inbox, "withdraw", receive_then_withdraw)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "read"
    assert inbox.inbox("sw-eng-3") == []


def test_task_mail_waits_until_a_live_successor_takes_the_seat(redis):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sw-eng-3", "eng-1@sw", "Finish the old work.", task="t1")
    inbox.deliver(item.id, "sw-eng-1")
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw", state="finished"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", item.created_at + 1)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "pending"
    assert inbox.inbox("sw-eng-3") == []


def test_withdraw_without_an_address_guard_cancels_the_open_message(redis):
    inbox = InboxStore(redis)
    item = inbox.send("alice", "bob", "Review this work.")
    closed = inbox.withdraw(item.id, "swarm", "cancelled: nobody takes the work")
    assert closed.state == "cancelled"
    assert closed.reason == "cancelled: nobody takes the work"
    assert inbox.get(item.id).state == "cancelled"


@pytest.mark.parametrize("receiver", ["bob", "XXXX"])
def test_withdraw_requires_a_recorded_receipt_for_the_expected_receiver(redis, receiver):
    inbox = InboxStore(redis)
    item = inbox.send("alice", receiver, "Review this work.")
    assert inbox.withdraw(item.id, "swarm", "cancelled: receiver left", expected_receiver=receiver) is None
    assert inbox.get(item.id).state == "pending"


def test_unreceived_task_mail_is_not_cancelled_as_a_new_occupant_receives_it(redis, monkeypatch):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat="eng-1@sw"))
    store.seats.occupy("eng-1@sw", "sw-eng-2", 1)
    item = inbox.send("sw-eng-3", "eng-1@sw", "Finish old work.", task="t1")
    withdraw = inbox.withdraw

    def receive_then_withdraw(*args, **kwargs):
        store.seats.occupy("eng-1@sw", "XXXX", item.created_at + 1)
        inbox.read(item.id, "XXXX")
        return withdraw(*args, **kwargs)

    monkeypatch.setattr(inbox, "withdraw", receive_then_withdraw)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "pending"
    assert inbox.inbox("sw-eng-3") == []


def test_the_sweep_skips_a_settled_agent_until_mail_reaches_it_again(redis, monkeypatch):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    settled, settle = [], exits.settle
    monkeypatch.setattr(exits, "settle", lambda inbox, name, *rest: settled.append(name) or settle(inbox, name, *rest))
    exits.sweep(inbox, "sw", store, dict)
    exits.sweep(inbox, "sw", store, dict)
    assert settled == ["sw-eng-1"]
    assert store.seats.exit_of("sw-eng-1") == {"seat": "eng-1@sw", "reason": "exited", "generation": 1}
    late = inbox.send("sender", "sw-eng-1", "late contract")
    exits.sweep(inbox, "sw", store, dict)
    exits.sweep(inbox, "sw", store, dict)
    assert settled == ["sw-eng-1", "sw-eng-1"]
    assert inbox.get(late.id).address == "eng-1@sw"


def test_the_sweep_reads_the_swarms_seats_once(redis, monkeypatch):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    reads, seated = [], store.seats.agent_seats
    monkeypatch.setattr(store.seats, "agent_seats", lambda slug: reads.append(slug) or seated(slug))
    exits.sweep(inbox, "sw", store, dict)
    assert reads == ["sw"]


def test_mail_passed_to_a_gone_successor_master_reaches_the_live_one_in_the_same_sweep(redis):
    from scripts.swarm.naming import NameRegistry
    from scripts.swarm.store import AgentRecord

    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    names = NameRegistry(redis)
    names.mint_code("sw", "sw", "/repo")
    first, second, live = (names.next("sw", "master") for _ in range(3))
    for at, name in enumerate((first, second, live), 1):
        store.seats.occupy("master@sw", name, at)
    store.put_agent("sw", AgentRecord(name=live, lane="master", task="master", seat="master@sw"))
    exits.sweep(inbox, "sw", store, dict)
    item = inbox.send("sender", first, "late")
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).address == live


@pytest.mark.parametrize(
    ("state", "exit_text"), [("done", "finished its task and exited"), ("blocked", "blocked its task and exited")]
)
def test_the_sweep_settles_a_closed_tasks_agent_with_its_task_outcome(redis, state, exit_text):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sender", "sw-eng-1", "contract")
    exits.sweep(inbox, "sw", store, lambda: {"t1": {"claimed_by": "sw-eng-1", "state": state}})
    assert store.seats.exit_of("sw-eng-1") == {"seat": "", "reason": exit_text, "generation": 1}
    assert inbox.get(item.id).state == "cancelled"
    assert inbox.get(item.id).reason == f"cancelled: sw-eng-1 {exit_text} before closing it"


def test_a_sweep_of_settled_agents_reads_the_open_index_once(redis, monkeypatch):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    for n in (1, 2, 3):
        store.seats.occupy(f"eng-{n}@sw", f"sw-eng-{n}", n)
    exits.sweep(inbox, "sw", store, dict)
    reads, quiet = [], inbox.quiet
    monkeypatch.setattr(inbox, "quiet", lambda names: reads.append(sorted(names)) or quiet(names))
    exits.sweep(inbox, "sw", store, dict)
    assert reads == [["sw-eng-1", "sw-eng-2", "sw-eng-3"]]


def test_each_gone_agent_is_settled_once_per_sweep(redis, monkeypatch):
    store, inbox = RedisStore(redis), InboxStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    for n in (1, 2, 3):
        store.seats.occupy(f"eng-{n}@sw", f"sw-eng-{n}", n)
    settled, settle = [], exits.settle
    monkeypatch.setattr(exits, "settle", lambda inbox, name, *rest: settled.append(name) or settle(inbox, name, *rest))
    exits.sweep(inbox, "sw", store, dict)
    assert sorted(settled) == ["sw-eng-1", "sw-eng-2", "sw-eng-3"]


def seat_notice_taken(inbox, store, state):
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("swarm", "eng-1@sw", "intent check for task t1", ref="tasks/t1")
    inbox.deliver(item.id, "sw-eng-1")
    if state == "confirmed":
        inbox.confirm(item.id, "sw-eng-1")
    elif state == "read":
        inbox.read(item.id, "sw-eng-1")
    return item


@pytest.mark.parametrize("state", ["delivered", "confirmed", "read"])
@pytest.mark.parametrize("path", ["settle", "sweep"])
def test_a_finished_agent_leaves_no_seat_mail_it_took_open(redis, state, path):
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = seat_notice_taken(inbox, store, state)
    if path == "settle":
        exits.settle(inbox, "sw-eng-1", "", "finished its task and exited")
    else:
        exits.sweep(inbox, "sw", store, lambda: {"t1": {"claimed_by": "sw-eng-1", "state": "done"}})
    settled = inbox.get(item.id)
    assert settled.state in CLOSED
    assert "sw-eng-1 finished its task and exited" in settled.reason
    assert inbox.pending_items("swarm") == []


@pytest.mark.parametrize("state", ["delivered", "confirmed", "read"])
def test_a_handing_off_agent_returns_the_seat_mail_it_took_to_its_next_occupant(redis, state):
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = seat_notice_taken(inbox, store, state)
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "handed off its seat")
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")
    assert inbox.pending_mail("sw-eng-1") == []
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    assert [mail.id for mail in inbox.pending_mail("sw-eng-2")] == [item.id]


def test_seat_mail_another_life_took_stays_with_it_when_an_agent_exits(redis):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    item = inbox.send("swarm", "eng-1@sw", "intent check for task t1", ref="tasks/t1")
    inbox.deliver(item.id, "sw-eng-2")
    exits.settle(inbox, "sw-eng-1", "", "finished its task and exited")
    assert inbox.get(item.id).state == "delivered"


def test_a_handing_off_agent_never_takes_its_own_mail_moved_to_its_seat(redis):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("sender", "sw-eng-1", "contract")
    inbox.deliver(item.id, "sw-eng-1")
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "handed off its seat")
    assert inbox.pending_mail("sw-eng-1") == []
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    assert [mail.id for mail in inbox.pending_mail("sw-eng-2")] == [item.id]


def test_a_life_resumed_into_its_seat_takes_seat_mail_again(redis):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "stopped")
    item = inbox.send("sender", "eng-1@sw", "contract")
    assert inbox.pending_mail("sw-eng-1") == []
    store.seats.occupy("eng-1@sw", "sw-eng-1", 2)
    assert [mail.id for mail in inbox.pending_mail("sw-eng-1")] == [item.id]


def test_a_resumed_life_that_hands_off_again_never_takes_its_moved_mail(redis):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "stopped")
    store.seats.occupy("eng-1@sw", "sw-eng-1", 2)
    item = inbox.send("sender", "sw-eng-1", "contract")
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "handed off its seat")
    assert inbox.pending_mail("sw-eng-1") == []
    assert store.seats.exit_of("sw-eng-1")["reason"] == "handed off its seat"
    store.seats.occupy("eng-1@sw", "sw-eng-2", 3)
    assert [mail.id for mail in inbox.pending_mail("sw-eng-2")] == [item.id]


def test_swarm_done_settles_the_seat_mail_its_agent_took(redis):
    from scripts.swarm import cli
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    item = seat_notice_taken(inbox, store, "confirmed")
    agent = AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw")
    store.put_agent("sw", agent)
    cli._retire(store, "sw", agent, "finished its task and exited")
    assert inbox.get(item.id).state == "cancelled"
    assert "sw-eng-1 finished its task and exited" in inbox.get(item.id).reason


@pytest.mark.parametrize("seat", ["", "eng-1@sw"])
def test_a_quota_notice_ends_with_the_life_it_was_sent_to(redis, seat):
    from scripts.swarm.quota_notice import HANDOFF, HURRY

    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    hurry = inbox.send("swarm", "sw-eng-1", HURRY)
    handoff = inbox.send("swarm", "sw-eng-1", HANDOFF.format(slug="sw"))
    inbox.deliver(hurry.id, "sw-eng-1")
    exits.settle(inbox, "sw-eng-1", seat, "handed off its seat")
    for item in (hurry, handoff):
        assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("sw-eng-1", "done")
        assert "quota notice ended" in inbox.get(item.id).reason
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    assert inbox.pending_mail("sw-eng-2") == []


@pytest.mark.parametrize(
    ("outcome", "state"), [("", "cancelled"), ("eng-1@sw", "pending")], ids=["finished", "handed off"]
)
def test_the_sweep_settles_seat_mail_a_settled_life_was_left_holding(redis, outcome, state):
    inbox, store = InboxStore(redis), RedisStore(redis)
    item = seat_notice_taken(inbox, store, "delivered")
    store.seats.record_exit("sw-eng-1", outcome, "finished its task and exited")
    store.seats.occupy("eng-1@sw", "sw-eng-2", 2)
    exits.sweep(inbox, "sw", store, dict)
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", state)
    assert "sw-eng-1 finished its task and exited" in inbox.get(item.id).reason
