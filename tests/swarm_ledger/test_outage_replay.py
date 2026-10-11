from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import watch_ledger
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

pytestmark = pytest.mark.unit

HOUR_MS = 60 * 60 * 1000
START = 1_800_000_000_000
CONTENT = {"title": "Outage", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}


@pytest.fixture
def clock():
    return SimpleNamespace(now=START)


@pytest.fixture
def repo(tmp_path, clock, monkeypatch):
    repo = SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    monkeypatch.setattr(repo.domain, "now_ms", lambda: clock.now)
    repo.create("ledger", CONTENT)
    return repo


def say(repo, *ids):
    state, rejected = repo.apply_ops("ledger", ops=[{"op": "add", "id": i, "thread": "chat", "text": i} for i in ids])
    assert rejected == []
    return state


def replay(repo, since, capsys):
    args = SimpleNamespace(all=False, name=None, slug="ledger", since_rev=since)
    watch_ledger.Watch(args, "ledger", None).show(repo.get_document("ledger"))
    return capsys.readouterr().out.splitlines()


def shown(lines):
    return [line.split("[", 1)[1].split("]", 1)[0] for line in lines if line.startswith("OPERATOR")]


def test_a_watcher_replays_every_operator_event_of_an_outage_longer_than_the_old_bound(repo, clock, capsys):
    cursor = say(repo, "before")["_meta"]["rev"]
    written = []
    for batch in range(21):
        clock.now += HOUR_MS // 2
        ids = [f"m{batch}-{n}" for n in range(100)]
        say(repo, *ids)
        written += ids
    assert len(written) > 2000
    lines = replay(repo, cursor, capsys)
    assert shown(lines) == written
    assert not [line for line in lines if line.startswith("WARNING")]


def test_events_older_than_the_window_fall_back_to_the_kept_count(repo, clock, monkeypatch):
    monkeypatch.setattr(repo.domain, "EVENTS_KEPT", 3)
    say(repo, "a", "b", "c", "d", "e")
    clock.now += repo.domain.EVENTS_WINDOW_MS + 1
    state = say(repo, "f")
    assert [e["id"] for e in state["_meta"]["events"]] == ["d", "e", "f"]


def test_a_watcher_behind_the_trimmed_history_is_told_which_revisions_it_lost(repo, monkeypatch, capsys):
    monkeypatch.setattr(repo.domain, "EVENTS_CEILING", 6)
    cursor = say(repo, "before")["_meta"]["rev"]
    revisions = [say(repo, f"m{n}")["_meta"]["rev"] for n in range(10)]
    lines = replay(repo, cursor, capsys)
    assert shown(lines) == [f"m{n}" for n in range(4, 10)]
    lost = (
        f"WARNING ledger history trimmed events after revision {cursor}"
        f" through revision {revisions[3]} before this watcher read them"
    )
    assert lines.count(lost) == 1
    assert lines.index(lost) < next(i for i, line in enumerate(lines) if line.startswith("OPERATOR"))


def test_a_watcher_ahead_of_the_trim_hears_no_warning(repo, monkeypatch, capsys):
    monkeypatch.setattr(repo.domain, "EVENTS_CEILING", 6)
    revisions = [say(repo, f"m{n}")["_meta"]["rev"] for n in range(10)]
    lines = replay(repo, revisions[3], capsys)
    assert shown(lines) == [f"m{n}" for n in range(4, 10)]
    assert not [line for line in lines if line.startswith("WARNING")]
