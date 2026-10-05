import pytest

from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord

pytestmark = pytest.mark.xdist_group("fakeredis")

W = 300_000
MASTER_NAME = "sw-master-1"


class FakeHerdr:
    def __init__(self, status):
        self.status, self.prompts = dict(status), []

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


class FakeLedger:
    def __init__(self):
        self.followups = []

    def followup(self, slug, text):
        self.followups.append((slug, text))


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


@pytest.mark.parametrize("state", ["blocked", "working", "unknown"])
def test_a_pane_that_is_not_idle_is_never_typed_into(inbox, state):
    item = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    herdr = FakeHerdr({"p1": state})
    for n in range(6):
        run(inbox, herdr, FakeLedger(), sent_at(item) + n * W)
    assert herdr.prompts == [] and events(inbox, item.id) == []


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


def test_a_delivered_or_closed_item_is_never_woken(inbox):
    delivered = inbox.send(MASTER_NAME, "sw-eng-1", "one")
    closed = inbox.send(MASTER_NAME, "sw-eng-1", "two")
    inbox.deliver(delivered.id, "sw-eng-1")
    inbox.close(closed.id, "sw-eng-1", "done")
    herdr, ledger = FakeHerdr({"p1": "idle"}), FakeLedger()
    for n in range(8):
        run(inbox, herdr, ledger, sent_at(delivered) + n * W)
    assert herdr.prompts == [] and ledger.followups == [] and inbox.inbox(MASTER_NAME) == []


def test_a_session_outside_herdr_gets_no_wake_and_goes_straight_to_escalation(inbox):
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
    assert len(herdr.prompts) == 3 and len(inbox.inbox(MASTER_NAME)) == 1 and len(ledger.followups) == 1
    assert events(inbox, item.id)[-1] == "escalated_operator"


def test_items_outside_the_swarm_are_left_alone(inbox):
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
