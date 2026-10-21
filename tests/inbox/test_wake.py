import pytest

from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle
from scripts.swarm.store import MASTER, AgentRecord

W = 300_000
MASTER_NAME = "sw-master-1"


class FakeHerdr:
    def __init__(self, status, typed=None):
        self.status, self.prompts, self.typed = dict(status), [], dict(typed or {})

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def typed_input(self, agent):
        return self.typed.get(agent.pane_id, "")

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


class FakeLedger:
    def __init__(self):
        self.followups, self.flagged = [], []

    def followup(self, slug, text, needs_operator=False):
        self.followups.append((slug, text))
        self.flagged.append(needs_operator)


AGENTS = [
    AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1"),
    AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm"),
]


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


def fresh(server):
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=server, decode_responses=True))


@pytest.fixture
def inbox(server):
    return fresh(server)


def run(inbox, herdr, ledger, now, agents=AGENTS):
    return wake.wake_pass(inbox, "sw", agents, herdr, ledger, now, W)


def events(inbox, item_id):
    return [e["event"] for e in inbox.history(item_id) if "event" in e]


def sent_at(item):
    return item.created_at


def test_an_idle_pane_with_a_pending_item_gets_exactly_one_prompt(inbox):
    item = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    second = inbox.send(MASTER_NAME, "sw-eng-1", "and the docs")
    herdr = FakeHerdr({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1)
    run(inbox, herdr, FakeLedger(), sent_at(item) + 2)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
    assert events(inbox, item.id) == ["woken"] and events(inbox, second.id) == ["woken"]


def test_many_pending_items_share_batched_wake_reads_without_changing_seen_or_fyi_behavior(inbox, monkeypatch):
    from scripts.inbox import seen
    from scripts.inbox.store import CLOSED

    items = [inbox.send(MASTER_NAME, "earlier-agent", f"notice {n}", ref=f"written:{n}", fyi=True) for n in range(40)]
    inbox.names.alias("earlier-agent", "sw-eng-1")
    assert seen.SeenMarks(inbox.redis).mark("sw-eng-1", items[0].ref)
    inbox.pending()
    reads = []
    execute = inbox.redis.execute_command

    def traced_execute(*args, **kwargs):
        if str(args[0]).upper() in {"GET", "HGETALL", "LRANGE", "SISMEMBER", "SMEMBERS"}:
            reads.append(args[0])
        return execute(*args, **kwargs)

    monkeypatch.setattr(inbox.redis, "execute_command", traced_execute)
    herdr = FakeHerdr({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(items[-1]) + 1)
    assert len(reads) <= 30
    assert inbox.get(items[0].id).state in CLOSED
    assert all(inbox.get(item.id).state == "pending" for item in items[1:])
    assert herdr.prompts == []


@pytest.mark.parametrize("state", ["blocked", "working", "unknown"])
def test_a_pane_that_is_not_idle_is_never_typed_into_and_escalates_after_one_window(inbox, state):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger = FakeHerdr({"p1": state}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W - 1)
    assert inbox.inbox(MASTER_NAME) == [] and events(inbox, item.id) == []
    for n in range(1, 6):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert herdr.prompts == []
    (raised,) = inbox.inbox(MASTER_NAME)
    assert item.id in raised.text and "review my diff" in raised.text
    assert events(inbox, item.id) == ["escalated_master", "escalated_operator"] and len(ledger.followups) == 1


@pytest.mark.parametrize("sender", ["swarm", "sw-eng-2"])
def test_actionable_notices_escalate_without_typing_into_a_busy_pane(inbox, sender):
    item = inbox.send(sender, "sw-eng-1", "finish the task")
    herdr, ledger = FakeHerdr({"p1": "working"}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W - 1)
    assert events(inbox, item.id) == []
    run(inbox, herdr, ledger, sent_at(item) + W)
    assert events(inbox, item.id) == ["escalated_master"]
    (raised,) = inbox.inbox(MASTER_NAME)
    assert raised.fyi is False
    [note] = [e for e in inbox.history(item.id) if e.get("event") == "escalated_master"]
    assert (note["by"], note["reason"]) == ("swarm", f"raised to {raised.address} as message {raised.id}")
    run(inbox, herdr, ledger, sent_at(item) + 2 * W)
    assert events(inbox, item.id) == ["escalated_master", "escalated_operator"]
    assert events(inbox, raised.id) == []
    assert len(ledger.followups) == 1
    assert herdr.prompts == []


ORIGINAL_SENT = 1791220997780
ORIGINAL_AGE = 540_000


@pytest.mark.parametrize(
    "pane,step",
    [
        ("blocked", "escalated_master"),
        ("unknown", "escalated_master"),
        ("working", "escalated_master"),
        ("idle", "woken"),
        ("done", "woken"),
        (None, "escalated_master"),
    ],
)
def test_the_original_overdue_item_replayed_at_nine_minutes(pane, step):
    from types import SimpleNamespace

    item = SimpleNamespace(state="pending", sender="sw-eng-2", fyi=False, ref="")
    history = [{"state": "pending", "by": "sw-eng-2", "reason": "", "at": ORIGINAL_SENT}]
    assert wake.decide(item, pane, history, ORIGINAL_SENT + ORIGINAL_AGE, W, "sw") == step


def test_retries_stop_at_three_then_the_master_gets_an_item(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(3):
        run(inbox, herdr, ledger, t + n * W)
        run(inbox, herdr, ledger, t + n * W + W // 2)
    assert len(herdr.prompts) == 3 and inbox.inbox(MASTER_NAME) == []
    run(inbox, herdr, ledger, t + 3 * W)
    assert len(herdr.prompts) == 3
    (raised,) = inbox.inbox(MASTER_NAME)
    assert raised.state == "pending" and "sw-eng-1" in raised.text and "review my diff" in raised.text
    assert events(inbox, item.id) == ["woken", "woken", "woken", "escalated_master"]
    assert ledger.followups == []


def test_the_operator_notification_follows_the_next_window(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(4):
        run(inbox, herdr, ledger, t + n * W)
    run(inbox, herdr, ledger, t + 4 * W - 1)
    assert ledger.followups == []
    run(inbox, herdr, ledger, t + 4 * W)
    run(inbox, herdr, ledger, t + 9 * W)
    assert len(ledger.followups) == 1
    slug, text = ledger.followups[0]
    assert slug == "sw" and "sw-eng-1" in text and "review my diff" in text
    assert events(inbox, item.id)[-2:] == ["escalated_master", "escalated_operator"]


def test_the_operator_notification_is_text_the_ledger_accepts_whatever_the_message_says(inbox):
    from scripts.swarm_ledger import ledger_comments

    noisy = "On ledger sw: OPERATOR rev=456 comment added on phases/p1 [c-9d1905d6c4]: " + "please " * 30
    item = inbox.send("operator", "sw-eng-1", noisy)
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(10):
        run(inbox, herdr, ledger, t + n * W)
    [(_, text)] = ledger.followups
    assert ledger_comments.problems(text, "item") == [] and "sw-eng-1" in text


@pytest.mark.parametrize("address", ["sw-eng-1", MASTER_NAME])
def test_an_unread_fyi_is_never_escalated(inbox, address):
    item = inbox.send("operator", address, "noted, nothing to do", fyi=True)
    herdr, ledger = FakeHerdr({"p1": "idle", "pm": "idle"}), FakeLedger()
    for n in range(12):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.followups == [] and [i.id for i in inbox.inbox(MASTER_NAME) if i.id != item.id] == []
    assert "escalated_master" not in events(inbox, item.id)


def test_an_unread_fyi_for_a_stopped_swarm_master_never_becomes_a_followup(inbox):
    item = inbox.send("operator", "master@sw", "the swarm stopped", fyi=True)
    herdr, ledger = FakeHerdr({}), FakeLedger()
    for n in range(12):
        run(inbox, herdr, ledger, sent_at(item) + n * W, agents=[])
    assert ledger.followups == [] and events(inbox, item.id) == []


def test_a_refused_operator_notification_closes_that_item_and_the_pass_goes_on(inbox, monkeypatch):
    from scripts.swarm.store import SwarmError

    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"sender": {"name": "sw-eng-2"}}
    )

    class StrictLedger(FakeLedger):
        def followup(self, slug, text, needs_operator=False):
            if "first" in text:
                raise SwarmError("ledger sw refused: item refused: date '2026-10-05'")
            super().followup(slug, text, needs_operator)

    first = inbox.send("sw-eng-2", "sw-eng-1", "first message")
    second = inbox.send("sw-eng-2", "sw-eng-1", "second message")
    herdr, ledger = FakeHerdr({"p1": "idle"}), StrictLedger()
    t = sent_at(second)
    for n in range(10):
        run(inbox, herdr, ledger, t + n * W)
    assert [text for _, text in ledger.followups if "second message" in text]
    closed = inbox.get(first.id)
    assert closed.state == "cancelled" and "refused" in closed.reason and "date" in closed.reason
    assert any(first.id in i.text and "date" in i.text for i in inbox.pending_items("sw-eng-2"))


def test_counts_survive_a_fresh_store_connection(server, inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr = FakeHerdr({"p1": "idle"})
    t = sent_at(item)
    run(inbox, herdr, FakeLedger(), t)
    run(fresh(server), herdr, FakeLedger(), t + W)
    run(fresh(server), herdr, FakeLedger(), t + 2 * W)
    run(fresh(server), herdr, FakeLedger(), t + 3 * W)
    assert len(herdr.prompts) == 3
    assert events(fresh(server), item.id)[-1] == "escalated_master"


def test_a_confirmed_or_closed_item_is_never_woken(inbox):
    delivered = inbox.send(MASTER_NAME, "sw-eng-1", "one")
    closed = inbox.send(MASTER_NAME, "sw-eng-1", "two")
    inbox.deliver(delivered.id, "sw-eng-1")
    inbox.confirm(delivered.id, "sw-eng-1")
    inbox.close(closed.id, "sw-eng-1", "done", "handled the request")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    for n in range(8):
        run(inbox, herdr, ledger, sent_at(delivered) + n * W)
    assert herdr.prompts == [] and ledger.followups == [] and inbox.inbox(MASTER_NAME) == []


def test_a_session_outside_herdr_gets_no_wake_and_goes_straight_to_escalation(inbox, monkeypatch):
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"desk": {"name": "operator-desk"}}
    )
    item = inbox.send("sw-eng-1", "operator-desk", "need a decision")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    t = sent_at(item)
    run(inbox, herdr, ledger, t + W - 1)
    assert inbox.inbox(MASTER_NAME) == []
    run(inbox, herdr, ledger, t + W)
    run(inbox, herdr, ledger, t + 2 * W)
    assert herdr.prompts == [] and len(inbox.inbox(MASTER_NAME)) == 1 and len(ledger.followups) == 1


def test_an_item_for_the_master_skips_straight_to_the_operator(inbox):
    item = inbox.send("sw-eng-1", MASTER_NAME, "check the plan")
    herdr, ledger = FakeHerdr({"pm": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(4):
        run(inbox, herdr, ledger, t + n * W)
    assert herdr.prompts == [] and len(inbox.inbox(MASTER_NAME)) == 1 and len(ledger.followups) == 1
    assert events(inbox, item.id) == ["escalated_operator"]


@pytest.mark.parametrize("address", ["sw-eng-1", MASTER_NAME])
def test_the_operator_follow_up_is_flagged_for_the_operator(inbox, address):
    item = inbox.send("sw-eng-2", address, "check the plan")
    herdr, ledger = FakeHerdr({"p1": "working", "pm": "idle"}), FakeLedger()
    for n in range(6):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert len(ledger.followups) == 1 and ledger.flagged == [True]


@pytest.mark.parametrize("harness", ["", "claude", "codex"])
def test_a_tick_with_pending_items_never_types_into_the_master_pane(inbox, harness):
    master = AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm", seat="master@sw", harness=harness)
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    first = inbox.send("sw-eng-1", MASTER_NAME, "check the plan")
    inbox.send("sw-eng-1", "master@sw", "and the seat item")
    inbox.send(wake.BY, MASTER_NAME, "a follow up waits on you")
    herdr = FakeHerdr({"pm": "idle", "p1": "idle"})
    for n in range(6):
        run(inbox, herdr, FakeLedger(), sent_at(first) + n * W, [AGENTS[0], master])
    assert herdr.prompts == []


def test_a_claude_pane_takes_its_items_through_the_inbox_channel_never_a_typed_wake(inbox):
    claude = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", harness="claude")
    codex = AgentRecord("sw-eng-2", "eng", "t2", pane_id="p2", harness="codex")
    item = inbox.send("sw-eng-4", "sw-eng-1", "review my diff")
    other = inbox.send("sw-eng-4", "sw-eng-2", "review mine")
    herdr = FakeHerdr({"p1": "idle", "p2": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1, [claude, codex, AGENTS[1]])
    assert herdr.prompts == [("p2", wake.WAKE_TEXT)]
    assert events(inbox, item.id) == [] and events(inbox, other.id) == ["woken"]
    run(inbox, herdr, FakeLedger(), sent_at(item) + W, [claude, codex, AGENTS[1]])
    assert events(inbox, item.id) == ["escalated_master"]


def test_a_codex_master_pane_is_never_woken_the_moment_an_item_lands(inbox):
    master = AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm", harness="codex")
    codex = AgentRecord("sw-eng-2", "eng", "t2", pane_id="p2", harness="codex")
    inbox.send("sw-eng-1", MASTER_NAME, "check the plan")
    item = inbox.send(MASTER_NAME, "sw-eng-2", "review mine")
    herdr = FakeHerdr({"pm": "idle", "p2": "idle"})
    wake.wake_now(inbox, "sw", [master, codex], herdr, sent_at(item) + 1, W)
    assert herdr.prompts == [("p2", wake.WAKE_TEXT)]


def test_the_wake_says_to_answer_through_the_inbox_never_in_the_terminal():
    assert "agentihooks msg reply" in wake.WAKE_TEXT
    assert "never as text in this terminal" in wake.WAKE_TEXT


def test_items_outside_the_swarm_are_left_alone(inbox, monkeypatch):
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"outside": {"name": "someone-else"}}
    )
    item = inbox.send("someone", "someone-else", "hi")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    for n in range(6):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert events(inbox, item.id) == [] and ledger.followups == []


def test_a_herdr_failure_records_no_wake(inbox):
    class Broken(FakeHerdr):
        def prompt(self, agent, text):
            raise TimeoutError("herdr hung")

    item = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    run(inbox, Broken({"p1": "idle"}), FakeLedger(), sent_at(item))
    assert events(inbox, item.id) == []


def test_the_window_comes_from_the_environment():
    assert wake.window_ms({}) == 300_000
    assert wake.window_ms({"AGENTIHOOKS_INBOX_RETRY_WINDOW_S": "60"}) == 60_000


SEATED = [
    AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat="eng-1@sw"),
    AgentRecord("sw-eng-4", "eng", "t2", pane_id="p4", seat="eng-2@sw"),
    AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm", seat="master@sw"),
]


def no_wake_session(monkeypatch):
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"desk": {"name": "operator-desk"}}
    )


def assert_raised_to_the_operator_only(inbox, item, ledger):
    assert inbox.inbox("master@sw") == [] and inbox.inbox(MASTER_NAME) == []
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.followups) == 1


@pytest.mark.parametrize("agents", [AGENTS, SEATED], ids=["unseated", "seat never occupied"])
def test_the_masters_own_message_nobody_reads_goes_to_the_operator_never_back_to_the_master(inbox, monkeypatch, agents):
    no_wake_session(monkeypatch)
    item = inbox.send(MASTER_NAME, "operator-desk", "need a decision")
    ledger = FakeLedger()
    run(inbox, FakeHerdr({}), ledger, sent_at(item) + W, agents)
    assert_raised_to_the_operator_only(inbox, item, ledger)


@pytest.mark.parametrize("sender", [MASTER_NAME, "sw-master-0"])
def test_a_master_seat_message_nobody_reads_goes_to_the_operator_never_back_to_its_seat(inbox, monkeypatch, sender):
    no_wake_session(monkeypatch)
    inbox.seats.occupy("master@sw", "sw-master-0", at=1)
    inbox.seats.occupy("master@sw", MASTER_NAME, at=2)
    earlier = AgentRecord("sw-master-0", "eng", "t9", seat="eng-3@sw")
    item = inbox.send(sender, "operator-desk", "need a decision")
    ledger = FakeLedger()
    run(inbox, FakeHerdr({}), ledger, sent_at(item) + W, [*SEATED, earlier])
    assert_raised_to_the_operator_only(inbox, item, ledger)


def test_an_agent_message_nobody_reads_still_goes_to_its_master_seat(inbox, monkeypatch):
    no_wake_session(monkeypatch)
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    item = inbox.send("sw-eng-1", "operator-desk", "need a decision")
    ledger = FakeLedger()
    run(inbox, FakeHerdr({}), ledger, sent_at(item) + W, SEATED)
    [raised] = inbox.inbox("master@sw")
    assert item.id in raised.text and events(inbox, item.id) == ["escalated_master"] and ledger.followups == []


def test_an_item_for_a_seat_wakes_the_pane_of_its_occupant(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    item = inbox.send(MASTER_NAME, "eng-1@sw", "rebase please")
    herdr = FakeHerdr({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1, SEATED)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
    assert events(inbox, item.id) == ["woken"]


def test_a_wake_racing_a_handover_writes_no_wake_note(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    item = inbox.send(MASTER_NAME, "eng-1@sw", "rebase please")

    class HandoverHerdr(FakeHerdr):
        def prompt(self, agent, text):
            super().prompt(agent, text)
            inbox.seats.occupy("eng-1@sw", "sw-eng-4", at=1)

    herdr = HandoverHerdr({"p1": "idle", "p4": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1, SEATED)
    assert events(inbox, item.id) == []
    run(inbox, herdr, FakeLedger(), sent_at(item) + 2, SEATED)
    assert herdr.prompts[-1] == ("p4", wake.WAKE_TEXT)


def test_escalation_goes_to_the_master_seat(inbox):
    item = inbox.send("sw-eng-4", "sw-eng-1", "hello")
    outside = AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw")
    run(inbox, FakeHerdr({}), FakeLedger(), sent_at(item) + W, [outside, SEATED[2]])
    [raised] = inbox.inbox("master@sw")
    assert item.id in raised.text


@pytest.mark.parametrize("gone", ["absent", "exited"])
def test_seat_mail_waits_for_the_successor_and_its_window_starts_at_binding(inbox, gone):
    inbox.seats.occupy("eng-1@sw", "sw-eng-gone", at=1)
    waiting = SEATED[1:]
    if gone == "exited":
        inbox.seats.record_exit("sw-eng-gone", "eng-1@sw", "handed off its seat")
        waiting = [AgentRecord("sw-eng-gone", "eng", "t1", pane_id="pg", seat="eng-1@sw"), *waiting]
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    for n in range(1, 30):
        run(inbox, herdr, ledger, sent_at(item) + n * W, waiting)
    assert events(inbox, item.id) == [] and inbox.inbox("master@sw") == [] and ledger.followups == []
    bound = sent_at(item) + 30 * W
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=bound)
    run(inbox, herdr, ledger, bound + W - 1, SEATED)
    assert events(inbox, item.id) == []
    run(inbox, herdr, ledger, bound + W, SEATED)
    assert events(inbox, item.id) == ["escalated_master"]


def test_master_seat_mail_waits_for_the_next_master(inbox):
    inbox.seats.occupy("master@sw", "sw-master-0", at=1)
    item = inbox.send("sw-eng-4", "master@sw", "check the plan")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    for n in range(1, 10):
        run(inbox, herdr, ledger, sent_at(item) + n * W, SEATED[:2])
    assert events(inbox, item.id) == [] and ledger.followups == []
    bound = sent_at(item) + 10 * W
    inbox.seats.occupy("master@sw", MASTER_NAME, at=bound)
    run(inbox, herdr, ledger, bound + W - 1, SEATED)
    assert ledger.followups == []
    run(inbox, herdr, ledger, bound + W, SEATED)
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.followups) == 1


def test_master_seat_mail_on_a_stopped_swarm_escalates_as_before(inbox):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(inbox.redis)
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    store.update("sw", state="stopped")
    inbox.seats.occupy("master@sw", "sw-master-0", at=1)
    item = inbox.send("sw-eng-4", "master@sw", "check the plan")
    ledger = FakeLedger()
    run(inbox, FakeHerdr({}), ledger, sent_at(item) + W, SEATED[:2])
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.followups) == 1


def test_seat_mail_to_a_live_occupant_escalates_after_one_window(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    herdr, ledger = FakeHerdr({"p1": "working"}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W - 1, SEATED)
    assert events(inbox, item.id) == []
    run(inbox, herdr, ledger, sent_at(item) + W, SEATED)
    assert events(inbox, item.id) == ["escalated_master"]


def test_a_codex_successor_is_woken_one_window_after_it_binds(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-gone", at=1)
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    inbox.note(item.id, wake.WOKEN, wake.BY, "prompted sw-eng-gone to read its inbox", sent_at(item) + 1)
    bound = sent_at(item) + 10 * W
    codex = AgentRecord("sw-eng-2", "eng", "t1", pane_id="p2", harness="codex", seat="eng-1@sw")
    inbox.seats.occupy("eng-1@sw", codex.name, at=bound)
    herdr = FakeHerdr({"p2": "idle"})
    wake.wake_now(inbox, "sw", [codex], herdr, bound + W - 1, W)
    assert herdr.prompts == []
    wake.wake_now(inbox, "sw", [codex], herdr, bound + W, W)
    assert herdr.prompts == [("p2", wake.WAKE_TEXT)]


def swarm_config(inbox, state="running"):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(inbox.redis)
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    store.update("sw", state=state)


def test_the_window_counts_from_the_items_first_step_at_clock_zero(inbox, monkeypatch):
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: 0)
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    assert wake.decide(item, None, inbox.history(item.id), W, W, "sw") == wake.TO_MASTER
    run(inbox, FakeHerdr({"p1": "working"}), FakeLedger(), W)
    assert events(inbox, item.id) == ["escalated_master"]


def test_a_seat_note_after_binding_leaves_the_window_at_the_bind(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    bound = sent_at(item) + W
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=bound)
    inbox.seats.note("eng-1@sw", "promoted", "a later seat note", bound + W)
    run(inbox, FakeHerdr({"p1": "working"}), FakeLedger(), bound + W, SEATED)
    assert events(inbox, item.id) == ["escalated_master"]


def test_mail_on_a_never_seated_lane_of_a_stopped_swarm_waits_out_its_first_window(inbox):
    swarm_config(inbox, "stopped")
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    assert run(inbox, FakeHerdr({}), FakeLedger(), sent_at(item) + 1, SEATED[1:]) == []
    assert events(inbox, item.id) == []


@pytest.mark.parametrize(("seat", "held"), [("eng-1@sw", True), ("eng-2@sw", False)])
def test_a_running_swarm_holds_only_seats_inside_the_lane_cap(inbox, seat, held):
    swarm_config(inbox)
    inbox.seats.occupy(seat, "sw-eng-gone", at=1)
    item = inbox.send("sw-eng-4", seat, "rebase please")
    run(inbox, FakeHerdr({}), FakeLedger(), sent_at(item) + W, [SEATED[2]])
    assert events(inbox, item.id) == ([] if held else ["escalated_master"])


def test_a_handover_before_the_prompt_leaves_the_old_pane_alone(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    item = inbox.send(MASTER_NAME, "eng-1@sw", "rebase please")

    class HandoverOnStatus(FakeHerdr):
        def agent_status(self, agent):
            inbox.seats.occupy("eng-1@sw", "sw-eng-4", at=1)
            return super().agent_status(agent)

    herdr = HandoverOnStatus({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1, SEATED)
    assert herdr.prompts == [] and events(inbox, item.id) == []


def test_a_pane_holding_typed_input_is_left_alone_until_the_operator_sends_it(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "check the plan")
    herdr = FakeHerdr({"p1": "idle"}, {"p1": "wait, before you"})
    assert run(inbox, herdr, FakeLedger(), sent_at(item) + 1) == []
    assert herdr.prompts == [] and events(inbox, item.id) == []
    herdr.typed["p1"] = ""
    run(inbox, herdr, FakeLedger(), sent_at(item) + 2)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]


def test_typed_input_keeps_todays_escalation_timing(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger = FakeHerdr({"p1": "idle"}, {"p1": "half a sentence"}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W - 1)
    assert events(inbox, item.id) == []
    run(inbox, herdr, ledger, sent_at(item) + W)
    assert herdr.prompts == [] and events(inbox, item.id) == ["escalated_master"]


def test_a_pane_the_operator_prompted_inside_the_quiet_window_is_left_alone(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "check the plan")
    herdr = FakeHerdr({"p1": "idle"})
    idle.prompted(inbox.redis, "sw", "sw-eng-1", sent_at(item))
    assert run(inbox, herdr, FakeLedger(), sent_at(item) + wake.DEFAULT_QUIET_S * 1000 - 1) == []
    assert herdr.prompts == []
    run(inbox, herdr, FakeLedger(), sent_at(item) + wake.DEFAULT_QUIET_S * 1000)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]


def test_the_quiet_window_comes_from_the_environment():
    assert wake.quiet_ms({}) == wake.DEFAULT_QUIET_S * 1000
    assert wake.quiet_ms({wake.QUIET_ENV: "30"}) == 30_000


def test_the_raised_master_item_reads_back_from_its_escalation_note():
    note = wake.raised_note("master@sw", "abc123def456")
    assert note == "raised to master@sw as message abc123def456"
    assert wake.raised_id({"event": wake.TO_MASTER, "reason": note}) == "abc123def456"
    assert wake.raised_id({"event": wake.WOKEN, "reason": note}) == ""
    assert wake.raised_id({"state": "pending", "reason": note}) == ""


def test_an_unread_fyi_is_never_typed_as_a_wake_while_a_work_item_still_is(inbox):
    fyi = inbox.send(MASTER_NAME, "sw-eng-1", "noted, nothing to do", fyi=True)
    herdr = FakeHerdr({"p1": "idle"})
    for n in range(6):
        run(inbox, herdr, FakeLedger(), sent_at(fyi) + n * W + 1)
    assert herdr.prompts == [] and events(inbox, fyi.id) == []
    work = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    run(inbox, herdr, FakeLedger(), sent_at(work) + 1)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
    assert events(inbox, work.id) == ["woken"] and events(inbox, fyi.id) == []


def test_a_codex_pane_holding_only_fyi_mail_gets_no_wake(inbox):
    codex = AgentRecord("sw-eng-2", "eng", "t1", pane_id="p2", harness="codex")
    fyi = inbox.send(MASTER_NAME, codex.name, "noted, nothing to do", fyi=True)
    herdr = FakeHerdr({"p2": "idle"})
    wake.wake_now(inbox, "sw", [codex], herdr, sent_at(fyi) + 1, W)
    assert herdr.prompts == [] and events(inbox, fyi.id) == []


def test_fyi_seat_mail_waits_for_no_successor_and_gets_no_wake_after_binding(inbox):
    inbox.seats.occupy("eng-1@sw", "sw-eng-gone", at=1)
    fyi = inbox.send("sw-eng-4", "eng-1@sw", "your pull request merged", fyi=True)
    work = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    for n in range(1, 6):
        run(inbox, herdr, ledger, sent_at(fyi) + n * W, SEATED[1:])
    assert events(inbox, fyi.id) == events(inbox, work.id) == []
    bound = sent_at(fyi) + 10 * W
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=bound)
    for n in range(4):
        run(inbox, herdr, ledger, bound + n * W + 1, SEATED)
    assert events(inbox, fyi.id) == [] and ledger.followups == []
    assert events(inbox, work.id)[0] == "woken"
