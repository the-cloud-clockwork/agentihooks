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
        self.followups, self.alerts, self.alert_keys, self.cleared = [], [], [], []

    def followup(self, slug, text, needs_operator=False):
        self.followups.append((slug, text))

    def raise_alert(self, slug, source, text, item=None):
        self.alerts.append((slug, text))
        self.alert_keys.append((source, item))

    def close_item_alerts(self, slug, item, outcome):
        self.cleared.append((slug, item, outcome))


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


@pytest.mark.parametrize("age", [1, W + 1])
@pytest.mark.parametrize("seated", [False, True])
def test_many_pending_items_share_batched_wake_reads_without_changing_seen_or_fyi_behavior(
    inbox, monkeypatch, age, seated
):
    from dataclasses import replace

    from scripts.inbox import seen
    from scripts.inbox.store import CLOSED

    inbox.names.mint_code("sw", "sw", "/repo")
    canonical = inbox.names.next("sw", "eng")
    address = "eng-1@sw" if seated else "earlier-agent"
    if seated:
        inbox.seats.occupy(address, canonical, at=1)
    agents = [replace(agent, name=canonical) if agent.name == "sw-eng-1" else agent for agent in AGENTS]
    items = [inbox.send(MASTER_NAME, address, f"notice {n}", ref=f"written:{n}", fyi=True) for n in range(40)]
    if not seated:
        inbox.names.alias(address, canonical)
    assert seen.SeenMarks(inbox.redis).mark(canonical, items[0].ref)
    inbox.pending()
    reads = []
    execute = inbox.redis.execute_command

    def traced_execute(*args, **kwargs):
        if str(args[0]).upper() in {"GET", "HGETALL", "LRANGE", "SISMEMBER", "SMEMBERS"}:
            reads.append(args[0])
        return execute(*args, **kwargs)

    monkeypatch.setattr(inbox.redis, "execute_command", traced_execute)
    herdr = FakeHerdr({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(items[-1]) + age, agents)
    assert len(reads) <= 30
    assert inbox.get(items[0].id).state in CLOSED
    assert all(inbox.get(item.id).state == "pending" for item in items[1:])
    assert herdr.prompts == []


def test_bulk_seen_marks_keep_alias_pairs_and_empty_results(inbox, monkeypatch):
    from unittest.mock import Mock

    from scripts.inbox import seen

    inbox.names.mint_code("sw", "sw", "/repo")
    canonical = inbox.names.next("sw", "eng")
    inbox.names.alias("earlier-agent", canonical)
    marks = seen.SeenMarks(inbox.redis)
    assert marks.mark(canonical, "written")
    pipeline = Mock(wraps=inbox.redis.pipeline)
    monkeypatch.setattr(inbox.redis, "pipeline", pipeline)
    assert marks.seen_many([("earlier-agent", "written"), (canonical, "written"), (canonical, "other")]) == {
        ("earlier-agent", "written"),
        (canonical, "written"),
    }
    pipeline.assert_called_once_with(transaction=False)
    assert marks.seen_many([]) == set()
    pipeline.assert_called_once_with(transaction=False)


def test_bulk_message_histories_preserve_entries_and_empty_results(inbox, monkeypatch):
    from unittest.mock import Mock

    first = inbox.send(MASTER_NAME, "sw-eng-1", "first")
    second = inbox.send(MASTER_NAME, "sw-eng-1", "second")
    pipeline = Mock(wraps=inbox.redis.pipeline)
    monkeypatch.setattr(inbox.redis, "pipeline", pipeline)
    assert inbox.histories([first.id, second.id]) == {
        first.id: inbox.history(first.id),
        second.id: inbox.history(second.id),
    }
    pipeline.assert_called_once_with(transaction=False)
    assert inbox.histories([]) == {}
    pipeline.assert_called_once_with(transaction=False)


def test_batched_wake_resolves_an_old_address_to_the_current_pane(inbox):
    from dataclasses import replace

    inbox.names.mint_code("sw", "sw", "/repo")
    canonical = inbox.names.next("sw", "eng")
    agents = [replace(agent, name=canonical) if agent.name == "sw-eng-1" else agent for agent in AGENTS]
    item = inbox.send(MASTER_NAME, "earlier-agent", "work")
    inbox.names.alias("earlier-agent", canonical)
    herdr = FakeHerdr({"p1": "idle"})
    run(inbox, herdr, FakeLedger(), sent_at(item) + 1, agents)
    assert herdr.prompts == [("p1", wake.WAKE_TEXT)]
    assert events(inbox, item.id) == ["woken"]


def test_wake_inputs_resolve_senders_and_skip_seen_reads_without_a_ref_or_receiver(inbox):
    from unittest.mock import Mock

    inbox.names.mint_code("sw", "sw", "/repo")
    canonical = inbox.names.next("sw", "eng")
    first = inbox.send("earlier-agent", "outside", "from an old sender", ref="written")
    second = inbox.send("unknown", "sw-eng-1", "without a reference")
    third = inbox.send("unknown", "eng-2@sw", "waiting for an occupant", ref="waiting")
    fourth = inbox.send("unknown", "outside", "another swarm")
    inbox.names.alias("earlier-agent", canonical)
    marks = Mock()
    marks.seen_many.return_value = set()

    reads = wake._wake_inputs(inbox, "sw", [first, second, third, fourth], {canonical, "sw-eng-1"}, None, marks)

    assert [item.id for item in reads.items] == [first.id, second.id, third.id]
    assert reads.items[0].sender == canonical
    assert reads.receivers[third.address][0] == ""
    assert reads.seen == set()
    marks.seen_many.assert_called_once_with([("outside", "written")])


def test_unresolved_sweep_keeps_processing_after_repeated_live_addresses(inbox, monkeypatch):
    from unittest.mock import Mock

    from scripts.inbox import addresses

    first = inbox.send(MASTER_NAME, "sw-eng-1", "live recipient")
    second = inbox.send(MASTER_NAME, "sw-eng-1", "same live recipient")
    third = inbox.send(MASTER_NAME, "missing", "unresolved recipient")
    monkeypatch.setattr(addresses, "get_active_sessions", lambda **kwargs: {})
    resolves = Mock(wraps=addresses.resolves)
    monkeypatch.setattr(addresses, "resolves", resolves)

    actions = addresses.settle_unresolved(inbox, {"sw-eng-1"}, [first, second, third], sent_at(third) + W + 1, W)

    assert actions == [f"closed message {third.id}: recipient no longer resolves"]
    assert inbox.get(first.id).state == "pending"
    assert inbox.get(second.id).state == "pending"
    assert inbox.get(third.id).state == "cancelled"
    assert [call.args[1] for call in resolves.call_args_list] == ["sw-eng-1", "missing"]


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
    assert events(inbox, item.id) == ["escalated_master", "escalated_operator"] and len(ledger.alerts) == 1


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
    assert len(ledger.alerts) == 1
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
    assert ledger.alerts == []


def test_the_operator_notification_follows_the_next_window(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(4):
        run(inbox, herdr, ledger, t + n * W)
    run(inbox, herdr, ledger, t + 4 * W - 1)
    assert ledger.alerts == []
    run(inbox, herdr, ledger, t + 4 * W)
    run(inbox, herdr, ledger, t + 9 * W)
    assert len(ledger.alerts) == 1
    slug, text = ledger.alerts[0]
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
    [(_, text)] = ledger.alerts
    assert text.endswith(f"(message {item.id})") and "sw-eng-1" in text
    assert ledger_comments.problems(text.removesuffix(f" (message {item.id})"), "item") == []


@pytest.mark.parametrize("address", ["sw-eng-1", MASTER_NAME])
def test_an_unread_fyi_is_never_escalated(inbox, address):
    item = inbox.send("operator", address, "noted, nothing to do", fyi=True)
    herdr, ledger = FakeHerdr({"p1": "idle", "pm": "idle"}), FakeLedger()
    for n in range(12):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.alerts == [] and [i.id for i in inbox.inbox(MASTER_NAME) if i.id != item.id] == []
    assert "escalated_master" not in events(inbox, item.id)


def test_an_unread_fyi_for_a_stopped_swarm_master_never_becomes_a_followup(inbox):
    item = inbox.send("operator", "master@sw", "the swarm stopped", fyi=True)
    herdr, ledger = FakeHerdr({}), FakeLedger()
    for n in range(12):
        run(inbox, herdr, ledger, sent_at(item) + n * W, agents=[])
    assert ledger.alerts == [] and events(inbox, item.id) == []


def test_a_refused_operator_notification_closes_that_item_and_the_pass_goes_on(inbox, monkeypatch):
    from scripts.swarm.store import SwarmError

    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"sender": {"name": "sw-eng-2"}}
    )

    class StrictLedger(FakeLedger):
        def raise_alert(self, slug, source, text, item=None):
            if "first" in text:
                raise SwarmError("ledger sw refused: item refused: date '2026-10-05'")
            super().raise_alert(slug, source, text, item)

    first = inbox.send("sw-eng-2", "sw-eng-1", "first message")
    second = inbox.send("sw-eng-2", "sw-eng-1", "second message")
    herdr, ledger = FakeHerdr({"p1": "idle"}), StrictLedger()
    t = sent_at(second)
    for n in range(10):
        run(inbox, herdr, ledger, t + n * W)
    assert [text for _, text in ledger.alerts if "second message" in text]
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
    assert herdr.prompts == [] and ledger.alerts == [] and inbox.inbox(MASTER_NAME) == []


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
    assert herdr.prompts == [] and len(inbox.inbox(MASTER_NAME)) == 1 and len(ledger.alerts) == 1


def test_an_item_for_the_master_skips_straight_to_the_operator(inbox):
    item = inbox.send("sw-eng-1", MASTER_NAME, "check the plan")
    herdr, ledger = FakeHerdr({"pm": "idle"}), FakeLedger()
    t = sent_at(item)
    for n in range(4):
        run(inbox, herdr, ledger, t + n * W)
    assert herdr.prompts == [] and len(inbox.inbox(MASTER_NAME)) == 1 and len(ledger.alerts) == 1
    assert events(inbox, item.id) == ["escalated_operator"]


@pytest.mark.parametrize("address", ["sw-eng-1", MASTER_NAME])
def test_an_unread_item_past_its_last_escalation_raises_one_inbox_alert_and_no_follow_up(inbox, address):
    item = inbox.send("sw-eng-2", address, "check the plan")
    herdr, ledger = FakeHerdr({"p1": "working", "pm": "idle"}), FakeLedger()
    for n in range(12):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.alert_keys == [("inbox", item.id)] and ledger.followups == []
    assert inbox.redis.smembers(inbox.key(wake.ALERTED, "sw")) == {item.id} and ledger.cleared == []


def test_closing_an_alerted_item_clears_its_alert_once(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "check the plan")
    herdr, ledger = FakeHerdr({"p1": "working"}), FakeLedger()
    for n in range(4):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.alert_keys == [("inbox", item.id)]
    inbox.close(item.id, "sw-eng-1", "done", "read it")
    actions = run(inbox, herdr, ledger, sent_at(item) + 5 * W)
    run(inbox, herdr, ledger, sent_at(item) + 6 * W)
    assert ledger.cleared == [("sw", item.id, "The message was closed.")]
    assert f"cleared the ledger alert for message {item.id}" in actions
    assert inbox.redis.smembers(inbox.key(wake.ALERTED, "sw")) == set()


def test_an_open_item_keeps_its_alert_and_a_refused_clear_is_retried(inbox):
    from scripts.swarm.store import SwarmError

    class Refusing(FakeLedger):
        refuse = True

        def close_item_alerts(self, slug, item, outcome):
            if self.refuse:
                raise SwarmError("ledger sw is gone")
            super().close_item_alerts(slug, item, outcome)

    item = inbox.send("sw-eng-2", "sw-eng-1", "check the plan")
    herdr, ledger = FakeHerdr({"p1": "working"}), Refusing()
    for n in range(6):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.cleared == []
    inbox.close(item.id, "sw-eng-1", "done", "read it")
    run(inbox, herdr, ledger, sent_at(item) + 7 * W)
    assert ledger.cleared == [] and inbox.redis.smembers(inbox.key(wake.ALERTED, "sw")) == {item.id}
    ledger.refuse = False
    run(inbox, herdr, ledger, sent_at(item) + 8 * W)
    assert [i for _, i, _ in ledger.cleared] == [item.id]


def test_a_vanished_alerted_item_clears_its_alert(inbox):
    inbox.redis.sadd(inbox.key(wake.ALERTED, "sw"), "gone-item")
    ledger = FakeLedger()
    assert wake.clear_alerts(inbox, "sw", ledger) == ["cleared the ledger alert for message gone-item"]
    assert ledger.cleared == [("sw", "gone-item", "The message was closed.")]


def test_an_unread_alert_notice_never_raises_another_alert(inbox):
    from scripts.swarm_ledger import ledger_alerts

    item = inbox.send(ledger_alerts.SENDER, MASTER_NAME, "On ledger sw: alert al-1-0 (inbox): unread")
    herdr, ledger = FakeHerdr({"pm": "idle"}), FakeLedger()
    for n in range(8):
        run(inbox, herdr, ledger, sent_at(item) + n * W)
    assert ledger.alerts == [] and ledger.followups == []
    assert events(inbox, item.id) == ["escalated_operator"]


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
    assert events(inbox, item.id) == [] and ledger.alerts == []


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
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.alerts) == 1


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
    assert item.id in raised.text and events(inbox, item.id) == ["escalated_master"] and ledger.alerts == []


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
    assert events(inbox, item.id) == [] and inbox.inbox("master@sw") == [] and ledger.alerts == []
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
    assert events(inbox, item.id) == [] and ledger.alerts == []
    bound = sent_at(item) + 10 * W
    inbox.seats.occupy("master@sw", MASTER_NAME, at=bound)
    run(inbox, herdr, ledger, bound + W - 1, SEATED)
    assert ledger.alerts == []
    run(inbox, herdr, ledger, bound + W, SEATED)
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.alerts) == 1


def test_master_seat_mail_on_a_stopped_swarm_escalates_as_before(inbox):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(inbox.redis)
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    store.update("sw", state="stopped")
    inbox.seats.occupy("master@sw", "sw-master-0", at=1)
    item = inbox.send("sw-eng-4", "master@sw", "check the plan")
    ledger = FakeLedger()
    run(inbox, FakeHerdr({}), ledger, sent_at(item) + W, SEATED[:2])
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.alerts) == 1


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
    assert events(inbox, fyi.id) == [] and ledger.alerts == []
    assert events(inbox, work.id)[0] == "woken"


FRONT = AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm", seat="master@sw", input_prompt="Which phase next?")
BACKEND = AgentRecord("sw-master-2", MASTER, MASTER, pane_id="pm2", seat="master-2@sw")


@pytest.mark.parametrize("ref", ["", "inbox-escalation:abc"])
def test_a_front_door_frozen_on_a_prompt_passes_its_pending_mail_to_a_live_master(inbox, ref):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan", ref=ref)
    herdr, ledger = FakeHerdr({}), FakeLedger()
    agents = [AGENTS[0], FRONT, BACKEND]
    assert run(inbox, herdr, ledger, sent_at(item) + W - 1, agents) == []
    assert inbox.get(item.id).address == "master@sw"
    actions = run(inbox, herdr, ledger, sent_at(item) + W, agents)
    assert actions == [f"passed message {item.id} from {MASTER_NAME} to master-2@sw"]
    moved = inbox.get(item.id)
    assert (moved.address, moved.state) == ("master-2@sw", "pending")
    assert moved.reason == f"{MASTER_NAME} waits on an input prompt; passed to master-2@sw, a live master"
    assert ledger.alerts == [] and inbox.open_items("master@sw") == []
    assert [i.id for i in inbox.open_items("master-2@sw")] == [item.id]


def test_a_frozen_front_door_with_no_other_live_master_escalates_as_before(inbox):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W, [AGENTS[0], FRONT])
    assert inbox.get(item.id).address == "master@sw"
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.alerts) == 1


def test_a_front_door_not_waiting_on_a_prompt_keeps_its_mail(inbox):
    from dataclasses import replace

    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    run(inbox, herdr, ledger, sent_at(item) + W, [AGENTS[0], replace(FRONT, input_prompt=""), BACKEND])
    assert inbox.get(item.id).address == "master@sw"
    assert events(inbox, item.id) == ["escalated_operator"]


def test_a_frozen_backend_master_passes_its_mail_to_the_front_door(inbox):
    from dataclasses import replace

    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    item = inbox.send("sw-eng-1", "master-2@sw", "check the plan")
    agents = [AGENTS[0], replace(FRONT, input_prompt=""), replace(BACKEND, input_prompt="Continue?")]
    run(inbox, FakeHerdr({}), FakeLedger(), sent_at(item) + W, agents)
    assert inbox.get(item.id).address == "master@sw"


def test_passed_mail_a_live_master_leaves_unread_goes_to_the_operator_never_back_to_the_frozen_front_door(inbox):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan")
    herdr, ledger = FakeHerdr({}), FakeLedger()
    agents = [AGENTS[0], FRONT, BACKEND]
    run(inbox, herdr, ledger, sent_at(item) + W, agents)
    actions = run(inbox, herdr, ledger, sent_at(item) + 2 * W, agents)
    assert actions == [f"raised message {item.id} to the operator"]
    assert events(inbox, item.id) == ["escalated_operator"] and len(ledger.alerts) == 1
    assert inbox.open_items("master@sw") == []


def test_an_agents_unread_mail_escalates_to_a_live_master_while_the_front_door_is_frozen(inbox):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    inbox.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    eng = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat="eng-1@sw")
    item = inbox.send("sw-eng-4", "eng-1@sw", "rebase please")
    ledger = FakeLedger()
    actions = run(inbox, FakeHerdr({"p1": "working"}), ledger, sent_at(item) + W, [eng, FRONT, BACKEND])
    assert actions == [f"raised message {item.id} to master-2@sw"]
    assert inbox.open_items("master@sw") == [] and ledger.alerts == []
    [raised] = inbox.open_items("master-2@sw")
    assert raised.ref == f"inbox-escalation:{item.id}"


def test_every_due_item_of_a_frozen_front_door_passes_in_one_pass_by_the_swarm(inbox):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    first = inbox.send("sw-eng-1", "master@sw", "check the plan")
    second = inbox.send("sw-eng-1", "master@sw", "check the phase")
    actions = run(inbox, FakeHerdr({}), FakeLedger(), sent_at(second) + W, [AGENTS[0], FRONT, BACKEND])
    assert actions == [
        f"passed message {first.id} from {MASTER_NAME} to master-2@sw",
        f"passed message {second.id} from {MASTER_NAME} to master-2@sw",
    ]
    assert [inbox.history(i.id)[-1]["by"] for i in (first, second)] == ["swarm", "swarm"]


def test_a_frozen_front_doors_window_starts_when_it_took_the_seat(inbox):
    inbox.seats.occupy("master@sw", "sw-master-0", at=1)
    inbox.seats.occupy("master-2@sw", "sw-master-2", at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan")
    bound = sent_at(item) + 2 * W
    inbox.seats.occupy("master@sw", MASTER_NAME, at=bound)
    agents = [AGENTS[0], FRONT, BACKEND]
    assert run(inbox, FakeHerdr({}), FakeLedger(), bound + W - 1, agents) == []
    assert inbox.get(item.id).address == "master@sw"
    assert run(inbox, FakeHerdr({}), FakeLedger(), bound + W, agents) == [
        f"passed message {item.id} from {MASTER_NAME} to master-2@sw"
    ]


def test_a_pass_leaves_an_item_its_receiver_already_took(inbox):
    inbox.seats.occupy("master@sw", MASTER_NAME, at=1)
    item = inbox.send("sw-eng-1", "master@sw", "check the plan")
    assert inbox.deliver(item.id, MASTER_NAME).state == "delivered"
    assert wake._pass(inbox, item, MASTER_NAME, "master-2@sw") == []
    assert inbox.get(item.id).address == "master@sw"


def test_decide_passes_a_frozen_masters_item_only_once_its_window_is_due():
    from scripts.inbox.store import Item

    item = Item("i1", "sw-eng-1", "master@sw", "text", "pending", 0, 0)
    assert wake.decide(item, wake.FROZEN, [{"at": 0}], W - 1, W, "sw") is None
    assert wake.decide(item, wake.FROZEN, [{"at": 0}], W, W, "sw") == wake.PASSED
    assert wake.decide(item, wake.FROZEN, [{"at": 0, "event": wake.TO_OPERATOR}], W, W, "sw") is None
