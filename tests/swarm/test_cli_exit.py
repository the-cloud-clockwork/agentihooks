import copy

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord
from tests.swarm.test_cli import env, run  # noqa: F401


@pytest.mark.parametrize("case", ["obsolete_wait", "blocked_dependency"])
def test_a_claimless_worker_exits_and_its_seat_is_free_on_the_next_tick(env, monkeypatch, capsys, case):  # noqa: F811
    from scripts.swarm.tick import tick

    store, ledger, runtime = env
    run("sw", "create", "--repo", ".", "--max-eng-agents", "0", "--max-ci-agents", "0")
    name = store.next_name("sw", "eng", 1000)
    agent = AgentRecord(name, "eng", "t1", pane_id="w1:p1", seat="eng-1@sw", started_at=1000)
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, name, 1000)
    runtime.live.add(name)
    ledger.rows["t1"].update(state="open", claimed_by="", parked=True)
    if case == "obsolete_wait":
        ledger.rows["t1"].update(state="claimed", claimed_by="successor")
        assert store.claim("sw", "t1", "successor", 60_000)
    else:
        ledger.rows["t1"]["depends_on"] = ["t2"]
        ledger.rows["t2"].update(state="blocked")
    before = copy.deepcopy(ledger.rows)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setattr(cli, "now_ms", lambda: 5000)
    capsys.readouterr()

    assert run("sw", "exit") == 0
    assert capsys.readouterr().out == f'{{"exited": "{name}"}}\n'
    assert runtime.reaped == [name]
    assert name not in runtime.live
    assert store.agents("sw")[0].state == "finished"
    assert store.seats.exit_of(name) == {
        "seat": "",
        "reason": "finished its session without a claim",
        "generation": 1,
    }
    assert ledger.rows == before
    assert store.claimant("sw", "t1") == ("successor" if case == "obsolete_wait" else None)

    store.update("sw", state="stopped")
    assert tick("sw", store, ledger, runtime, now_ms=6000) == []
    assert store.agents("sw")[0].state == "finished"
    assert runtime.closed == []
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=6001)
    assert [a for a in store.agents("sw") if a.lane != "master"] == []
    assert runtime.closed == ["w1:p1"]
    assert ledger.rows == before
    assert store.claimant("sw", "t1") == ("successor" if case == "obsolete_wait" else None)


def test_exit_does_not_abandon_an_active_worker_claim(env, monkeypatch):  # noqa: F811
    store, _, runtime = env
    run("sw", "create", "--repo", ".")
    name = store.next_name("sw", "eng", 1000)
    agent = AgentRecord(name, "eng", "t1", seat="eng-1@sw", started_at=1000)
    store.put_agent("sw", agent)
    assert store.claim("sw", "t1", name, 60_000)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")

    assert run("sw", "exit") == 1
    assert store.agents("sw") == [agent]
    assert store.claimant("sw", "t1") == name
    assert runtime.reaped == []
