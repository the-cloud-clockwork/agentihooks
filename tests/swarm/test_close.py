import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def closing(env, monkeypatch, tmp_path):  # noqa: F811
    store, ledger, rt = env
    ledger.calls = []
    ledger.summarize = lambda slug, note, by: ledger.calls.append(("summary", note, by))
    ledger.mark_closed = lambda slug, by: ledger.calls.append(("closed", by))
    monkeypatch.setattr(cli.snapshot, "path", lambda slug: tmp_path / slug / "snapshot.json")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    return store, ledger, rt, tmp_path


def test_close_retires_each_agent_with_its_task_scratch_homes(closing, scratch):
    store, ledger, rt, tmp_path = closing
    homes = {a.name: scratch(a.task) for a in store.agents("sw")}
    assert run("sw", "close", "--now", "--note", "Done.") == 0
    assert rt.homes == homes


def test_close_writes_the_summary_snapshots_retires_everyone_and_marks_the_ledger_closed(closing, capsys):
    store, ledger, rt, tmp_path = closing
    store.culture.set("sw", "be kind")
    store.memory.learn("eng-1@sw", "engineer@a1b2c3-0001", "a lesson", 1, "note")
    assert {row["state"] for row in ledger.rows.values()} == {"claimed"}
    assert run("sw", "close", "--now", "--note", "It went well.") == 0
    assert ledger.calls == [("summary", "It went well.", "operator"), ("closed", "operator")]
    assert (tmp_path / "sw" / "snapshot.json").exists()
    assert sorted(rt.killed) == ["ci@a1b2c3-0001", "engineer@a1b2c3-0001", "master@a1b2c3-0001"]
    assert rt.killed[-1] == "master@a1b2c3-0001"
    assert store.agents("sw") == []
    assert {row["state"] for row in ledger.rows.values()} == {"open"}
    assert {row["claimed_by"] for row in ledger.rows.values()} == {""}
    config = store.config("sw")
    assert (config.state, config.repo) == ("stopped", "/repo")
    assert store.culture.get("sw") == "be kind"
    assert [n["text"] for n in store.memory.learned("eng-1@sw")] == ["a lesson"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["closed"] == "sw"


def test_close_works_with_no_agents_and_no_note(closing):
    store, ledger, rt, _ = closing
    run("sw", "stop", "--now")
    ledger.calls.clear()
    assert run("sw", "close") == 0
    assert ledger.calls == [("summary", "", "operator"), ("closed", "operator")]
    assert store.config("sw").state == "stopped"


def test_the_master_running_close_signs_the_summary(closing, monkeypatch):
    _, ledger, _, _ = closing
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1b2c3-0001")
    assert run("sw", "close", "--note", "Done here.") == 0
    assert ledger.calls == [("summary", "Done here.", "master@a1b2c3-0001"), ("closed", "master@a1b2c3-0001")]


def test_close_hands_itself_to_a_live_master_and_changes_nothing_else(closing, capsys):
    store, ledger, rt, tmp_path = closing
    assert run("sw", "close") == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out == {"swarm": "sw", "asked": "master@a1b2c3-0001"}
    items = [i for i in InboxStore(store.redis).inbox("master@a1b2c3-0001") if i.sender == "operator"]
    assert len(items) == 1 and "agentihooks swarm sw close --note" in items[0].text
    assert ledger.calls == [] and rt.killed == []
    assert not (tmp_path / "sw" / "snapshot.json").exists()
    assert store.config("sw").state == "running"


def test_close_runs_at_once_when_no_master_is_live(closing):
    store, ledger, rt, _ = closing
    rt.live.discard("master@a1b2c3-0001")
    assert run("sw", "close") == 0
    assert [c[0] for c in ledger.calls] == ["summary", "closed"]
    assert store.agents("sw") == []
