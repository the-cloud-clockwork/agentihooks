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
