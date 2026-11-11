from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord
from tests.swarm.test_cli import env, run  # noqa: F401

SELF_EXIT = cli.cmd_exit.__name__.removeprefix("cmd_")


@pytest.mark.feature("swarm-exit-after-release")
def test_a_released_worker_exits_and_the_next_tick_retires_it(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.swarm.tick import tick

    store, ledger, runtime = env
    run("sw", "create", "--repo", ".", "--max-eng-agents", "0", "--max-ci-agents", "0")
    name = store.next_name("sw", "eng", 1000)
    agent = AgentRecord(name, "eng", "t1", pane_id="w1:p1", seat="eng-1@sw", started_at=1000, state="finished")
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, name, 1000)
    runtime.live.add(name)
    assert store.claim("sw", "t1", name, 60_000)
    ledger.rows["t1"].update(state="pr", claimed_by=name)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    capsys.readouterr()

    assert run("sw", SELF_EXIT) == 0
    assert capsys.readouterr().out == f'{{"exited": "{name}"}}\n'
    assert runtime.reaped == [name]
    assert store.claimant("sw", "t1") == name
    assert ledger.rows["t1"]["claimed_by"] == name

    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=6001)
    assert [a for a in store.agents("sw") if a.lane != "master"] == []
    assert runtime.closed == ["w1:p1"]
