import json
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore
from tests.helpers.seat import seat_owner
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_close import closing  # noqa: F401
from tests.swarm.test_tick import FakeRuntime


@pytest.fixture
def closed(closing, monkeypatch):  # noqa: F811
    store, ledger, rt, root = closing
    ledger.reopen = lambda slug, by: ledger.calls.append(("reopened", by))
    monkeypatch.setattr(cli.snapshot, "newest", lambda slug: root / slug / "snapshot.json")
    store.update("sw", lanes={"eng": {"model": "opus"}}, autonomy="full", max_eng=2)
    store.culture.set("sw", "Keep the record")
    store.memory.learn("master@sw", "master@a1b2c3-0001", "Carry the summary", 1)
    run("sw", "close", "--now")
    ledger.calls.clear()
    return store, ledger, rt, root


@pytest.mark.parametrize("removed", [False, True])
def test_reopen_keeps_settings_and_starts_a_new_master_with_only_open_work(closed, removed, capsys):
    store, ledger, rt, _ = closed
    ledger.rows["t1"].update(state="done", done=True)
    ledger.rows["t2"].update(state="open", depends_on=["missing"])
    if removed:
        store.remove("sw")
    assert run("sw", "reopen") == 0
    config = store.config("sw")
    assert config.state == "drained"
    assert (config.max_eng, config.lanes, config.autonomy) == (2, {"eng": {"model": "opus"}}, "full")
    assert store.culture.get("sw") == "Keep the record"
    assert [x["text"] for x in store.memory.learned("master@sw")] == ["Carry the summary"]
    assert [(a.name, a.seat) for a in store.agents("sw")] == [("master@a1b2c3-0002", "master@sw")]
    assert ledger.calls == [("reopened", "operator")]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["swarm"] == "sw"


def test_reopen_claims_open_tasks_only(closed: tuple[RedisStore, Any, FakeRuntime, Path]) -> None:
    store, ledger, rt, _ = closed
    ledger.rows["t2"].update(state="done", done=True)
    assert run("sw", "reopen") == 0
    seat_owner(store, ledger, rt, "t1", 2_000)
    assert {a.task for a in store.agents("sw")} == {"master", "t1"}
    assert ledger.rows["t2"]["state"] == "done"


def test_reopen_refuses_while_an_old_master_is_still_alive(closed):
    store, ledger, rt, _ = closed
    from scripts.swarm.store import AgentRecord

    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "master"))
    rt.live.add("master@a1b2c3-0001")
    assert run("sw", "reopen") == 1
    assert store.config("sw").state == "stopped"
    assert ledger.calls == []
