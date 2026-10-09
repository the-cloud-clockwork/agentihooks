import pytest

from scripts.inbox.store import InboxStore
from scripts.inbox.wake import wake_pass
from scripts.swarm.ledger_events import event_pass
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


class Ledger:
    def __init__(self):
        self.doc = {"_meta": {"rev": 1, "events": []}, "followups": [], "questions": [], "tasks": []}
        self.followups, self.priorities, self.now = [], [], 0

    def state(self, slug):
        return self.doc

    def followup(self, slug, text, needs_operator=False):
        self.followups.append(text)
        meta = self.doc["_meta"]
        meta["rev"] += 1
        row = {"id": f"f{meta['rev']}", "text": text, "comments": [], "done": False}
        self.doc["followups"].append({**row, **({"needs_operator": True} if needs_operator else {})})
        added = {"rev": meta["rev"], "at": self.now, "by": "swarm", "kind": "added", "text": text}
        meta["events"].append({**added, "target": f"followups/{row['id']}"})

    def priority(self, slug, item, text):
        self.priorities.append(item)


class Herdr:
    def __init__(self):
        self.prompts = []

    def agent_status(self, agent):
        return "idle"

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


@pytest.fixture
def crew():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    boss = AgentRecord("sw-master-1", "master", "master", seat="master@sw", pane_id="pm")
    store.put_agent("sw", boss)
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1"))
    inbox = InboxStore(store.redis)
    inbox.seats.occupy("master@sw", boss.name, at=1)
    ledger, herdr = Ledger(), Herdr()
    event_pass(inbox, store, "sw", ledger.doc, ledger, 1000)
    return store, inbox, ledger, herdr


def queue_event(crew, collection="followups", kind="added"):
    store, inbox, ledger, _ = crew
    row = {"id": "one", "text": "Decide the retry cap", "done": False}
    ledger.doc[collection] = [row]
    ledger.doc["_meta"] = {
        "rev": 2,
        "events": [
            {"rev": 2, "at": 1000, "by": "sw-eng-1", "kind": kind, "target": f"{collection}/one", "text": row["text"]}
        ],
    }
    event_pass(inbox, store, "sw", ledger.doc, ledger, 1000)
    return inbox.inbox("master@sw")[0]


def wake(crew, at):
    store, inbox, ledger, herdr = crew
    return wake_pass(inbox, "sw", store.agents("sw"), herdr, ledger, at, 300_000)


def test_a_followup_closed_before_the_wake_never_reaches_the_master(crew):
    _, inbox, ledger, herdr = crew
    item = queue_event(crew)
    ledger.doc["followups"][0]["done"] = True
    wake(crew, item.created_at + 1)
    assert inbox.pending() == []
    assert inbox.get(item.id).state == "done"
    assert herdr.prompts == []
    assert ledger.followups == []


def test_an_open_followup_stays_pending_for_the_master_without_a_typed_wake(crew):
    _, inbox, _, herdr = crew
    item = queue_event(crew)
    wake(crew, item.created_at + 1)
    assert inbox.pending() == [item]
    assert herdr.prompts == []


@pytest.mark.parametrize(
    ("collection", "kind", "fields"),
    [
        ("questions", "added", {"answers": [{"text": "Use port 8765", "by": "operator"}]}),
    ],
)
def test_a_decided_question_closes_before_a_wake(crew, collection, kind, fields):
    _, inbox, ledger, herdr = crew
    item = queue_event(crew, collection, kind)
    ledger.doc[collection][0].update(fields)
    wake(crew, item.created_at + 1)
    assert inbox.pending() == []
    assert inbox.get(item.id).state == "done"
    assert herdr.prompts == []


@pytest.mark.parametrize(
    ("collection", "kind", "fields"),
    [
        ("followups", "added", {"needs_operator": True}),
        ("questions", "added", {"out_of_scope": True}),
    ],
)
def test_other_ledger_decisions_close_the_event(crew, collection, kind, fields):
    _, inbox, ledger, herdr = crew
    item = queue_event(crew, collection, kind)
    ledger.doc[collection][0].update(fields)
    wake(crew, item.created_at + 300_001)
    assert inbox.pending() == []
    assert herdr.prompts == []
    assert ledger.followups == []


@pytest.mark.parametrize(
    ("collection", "kind", "fields"),
    [
        ("questions", "added", {}),
        ("questions", "added", {"answers": [{"text": "Removed answer", "deleted": True}]}),
        ("tasks", "task blocked", {"state": "blocked"}),
        ("tasks", "task blocked", {"state": "done", "done": True}),
        ("tasks", "task done", {"state": "done", "done": True}),
    ],
)
def test_undecided_questions_and_task_items_stay_pending_without_a_typed_wake(crew, collection, kind, fields):
    _, inbox, ledger, herdr = crew
    item = queue_event(crew, collection, kind)
    ledger.doc[collection][0].update(fields)
    wake(crew, item.created_at + 1)
    assert inbox.get(item.id).state == "pending"
    assert herdr.prompts == []


def test_a_closed_followup_reminder_also_closes_before_a_wake(crew):
    store, inbox, ledger, herdr = crew
    first = queue_event(crew)
    inbox.close(first.id, "swarm", "done", "initial notice handled")
    event_pass(inbox, store, "sw", ledger.doc, ledger, 901_000)
    reminder = inbox.pending()[0]
    ledger.doc["followups"][0]["done"] = True
    wake(crew, reminder.created_at + 1)
    assert inbox.pending() == []
    assert herdr.prompts == []


def test_a_decided_event_closes_even_without_a_master_occupant(crew):
    store, inbox, ledger, herdr = crew
    item = queue_event(crew)
    inbox.seats.occupy("master@sw", "", at=2)
    ledger.doc["followups"][0]["done"] = True
    wake_pass(inbox, "sw", [], herdr, ledger, item.created_at + 1, 300_000)
    assert inbox.pending() == []
    assert herdr.prompts == []


def test_an_unreferenced_message_is_preserved(crew):
    _, inbox, ledger, herdr = crew
    queue_event(crew)
    ledger.doc["followups"][0]["done"] = True
    item = inbox.send("swarm", "master@sw", "Check the deployment")
    wake(crew, item.created_at + 1)
    assert inbox.pending() == [item]
    assert herdr.prompts == []


def test_an_unread_master_item_yields_one_follow_up_that_never_returns_to_the_master(crew):
    store, inbox, ledger, _ = crew
    item = inbox.send("sw-eng-1", "master@sw", "Check the deployment")
    for n in range(1, 25):
        ledger.now = item.created_at + n * 300_000
        wake(crew, ledger.now)
        event_pass(inbox, store, "sw", ledger.doc, ledger, ledger.now)
    assert len(ledger.followups) == 1
    assert [i.id for i in inbox.pending()] == [item.id]
