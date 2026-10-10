import pytest

from scripts.swarm import metrics_ledger, metrics_outbox
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger import ledger_events_ack
from scripts.swarm_ledger.repository import events, mutation, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000
CONTENT = {"title": "Retention", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}


def chat(n):
    return {"op": "add", "id": f"m{n}", "thread": "chat", "text": f"message {n}"}


def ack(revision, by="swarm"):
    return {"op": "events_ack", "id": f"ack{revision}", "by": by, "rev": revision}


def chats(state):
    return [event["id"] for event in state["_meta"]["events"] if event.get("id", "").startswith("m")]


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledgers" / sqlite.DATABASE


@pytest.fixture
def repo(path, monkeypatch):
    repo = SQLiteLedgerRepository(path)
    monkeypatch.setattr(repo.domain, "EVENTS_KEPT", 3)
    monkeypatch.setattr(repo.domain, "EVENTS_CEILING", 6)
    monkeypatch.setattr(repo.domain, "now_ms", lambda: NOW - 1000)
    repo.create("ledger", CONTENT)
    return repo


def write(repo, *numbers):
    revisions = {}
    for n in numbers:
        state, rejected = repo.apply_ops("ledger", ops=[chat(n)])
        assert rejected == []
        revisions[n] = state["_meta"]["rev"]
    return state, revisions


def acknowledge(repo, revision):
    state, rejected = repo.apply_ops("ledger", ops=[ack(revision)])
    assert rejected == []
    return state


def assert_stored(path, state):
    fresh = SQLiteLedgerRepository(path)
    assert fresh.get_document("ledger") == state
    assert fresh.events_since("ledger", -1) == state["_meta"]["events"]


def test_without_an_acknowledgement_retention_keeps_the_newest_events(repo, path):
    state, revisions = write(repo, 1, 2, 3, 4, 5)
    assert len(state["_meta"]["events"]) == 3
    assert chats(state) == ["m3", "m4", "m5"]
    assert "events_ack" not in state["_meta"]
    assert state["_meta"]["events_trimmed"] == revisions[2]
    assert_stored(path, state)


def test_unacknowledged_events_outlive_normal_retention_until_acknowledged(repo, path):
    state, _ = write(repo, 1)
    acknowledge(repo, state["_meta"]["rev"])
    state, _ = write(repo, 2, 3, 4, 5, 6)
    assert chats(state) == ["m2", "m3", "m4", "m5", "m6"]
    assert_stored(path, state)
    state = acknowledge(repo, state["_meta"]["rev"])
    assert len(state["_meta"]["events"]) == 3
    assert chats(state) == ["m4", "m5", "m6"]
    assert_stored(path, state)


def test_the_ceiling_trims_unacknowledged_events_and_marks_the_highest_lost(repo, path):
    state, _ = write(repo, 1)
    acknowledge(repo, state["_meta"]["rev"])
    state, revisions = write(repo, *range(2, 11))
    assert len(state["_meta"]["events"]) == 6
    assert chats(state) == [f"m{n}" for n in range(5, 11)]
    assert state["_meta"]["events_trimmed"] == revisions[4]
    assert_stored(path, state)


def test_an_acknowledgement_is_monotonic_and_records_no_event(repo):
    state, revisions = write(repo, 1, 2)
    before = state["_meta"]["events"]
    state = acknowledge(repo, revisions[2])
    assert state["_meta"]["events_ack"] == revisions[2]
    assert state["_meta"]["events"] == before
    state = acknowledge(repo, revisions[1])
    assert state["_meta"]["events_ack"] == revisions[2]


def test_an_acknowledgement_never_passes_the_head_revision(repo):
    state, _ = write(repo, 1)
    head = state["_meta"]["rev"]
    state = acknowledge(repo, head + 50)
    assert state["_meta"]["events_ack"] == head


@pytest.mark.parametrize(
    "operation",
    [ack(1, by="eng"), ack(-1), ack(True), {"op": "events_ack", "id": "a", "by": "swarm"}],
)
def test_only_the_swarm_acknowledges_a_nonnegative_revision(repo, operation):
    with pytest.raises(ValueError):
        repo.domain.check_body({"ops": [operation]})
    with pytest.raises(ValueError):
        ledger_events_ack.check(operation)


def test_retained_drops_only_a_prefix_bounded_by_the_ceiling():
    rows = [{"rev": n} for n in (1, 2, 2, 3, 4, 5)]
    assert events.retained(rows, None, 2, 4) == 4
    assert events.retained(rows, 1, 2, 4) == 2
    assert events.retained(rows, 2, 2, 5) == 3
    assert events.retained(rows, 0, 2, 5) == 1
    assert events.retained(rows[:2], 0, 2, 5) == 0


def test_a_mutation_trims_acknowledged_events_and_marks_the_highest_trimmed(repo):
    state, revisions = write(repo, 1, 2, 3, 4)
    state["_meta"]["events_ack"] = revisions[2]
    rejected, _ = mutation.apply("ledger", state, repo.domain, ops=[chat(5)])
    assert rejected == []
    assert chats(state) == ["m3", "m4", "m5"]
    assert state["_meta"]["events_trimmed"] == revisions[2]


def test_the_client_acknowledges_through_the_swarm_op(monkeypatch):
    client, calls = LedgerClient(), []
    monkeypatch.setattr(client, "_call", lambda slug, ops: calls.append((slug, ops)))
    client.ack_events("ledger", 7)
    [(slug, [operation])] = calls
    assert slug == "ledger"
    assert {key: operation[key] for key in ("op", "by", "rev")} == {"op": "events_ack", "by": "swarm", "rev": 7}
    ledger_events_ack.check(operation)


class Client:
    def __init__(self, repo):
        self.repo = repo

    def state(self, slug):
        return self.repo.get_document(slug)

    def ack_events(self, slug, revision):
        acknowledge(self.repo, revision)


def gaps(box):
    return [row for row in box.recent("ledger_events", NOW + 10_000) if row["kind"] == "history gap"]


def test_the_metrics_pass_acknowledges_and_only_the_ceiling_trim_yields_a_gap(repo, tmp_path):
    box = metrics_outbox.Outbox(tmp_path / "outbox.sqlite", metrics_outbox.Settings("http://sink", "", ""))
    client = Client(repo)
    try:
        state, _ = write(repo, 1)
        metrics_ledger.record(box, "ledger", NOW, client)
        cursor = state["_meta"]["rev"]
        state = repo.get_document("ledger")
        assert state["_meta"]["events_ack"] == cursor
        metrics_ledger.record(box, "ledger", NOW + 1, client)
        assert repo.get_document("ledger")["_meta"]["rev"] == state["_meta"]["rev"]

        state, _ = write(repo, 2, 3, 4, 5, 6)
        assert chats(state) == ["m2", "m3", "m4", "m5", "m6"]
        metrics_ledger.record(box, "ledger", NOW + 2, client)
        assert gaps(box) == []
        assert repo.get_document("ledger")["_meta"]["events_ack"] == state["_meta"]["rev"]
        cursor = state["_meta"]["rev"]

        state, revisions = write(repo, *range(7, 16))
        assert state["_meta"]["events_trimmed"] == revisions[9]
        metrics_ledger.record(box, "ledger", NOW + 3, client)
        [gap] = gaps(box)
        assert gap["first_missed"] == cursor + 1 and gap["last_missed"] == revisions[9]
        assert gap["event_id"] == f"gap:ledger:{cursor + 1}:{revisions[9]}"
        shipped = [row["payload"] for row in box.recent("ledger_events", NOW + 10_000) if row["kind"] != "history gap"]
        assert sum('"m10"' in payload for payload in shipped) == 1
        assert not any('"m9"' in payload for payload in shipped)
    finally:
        box.close()
