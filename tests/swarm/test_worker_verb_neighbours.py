"""Who the swarm's worker verbs take today: a worker passes, with its task gates before a merge queue step, and the
master is told it works no task."""

from pathlib import Path

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

ENGINEER, MASTER = "engineer@a1b2c3-0001", "master@a1b2c3-0001"
PULL = "https://github.com/o/r/pull/9"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RedisStore:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)
    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", state="running", max_eng=1, max_ci=0))
    found.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1", seat="eng-1@sw", state="working"))
    found.put_agent("sw", AgentRecord(MASTER, "master", "master", seat="master@sw", state="working"))
    monkeypatch.setattr(cli, "connect", lambda: found)
    return found


def ran(monkeypatch: pytest.MonkeyPatch, name: str, *argv: str) -> int:
    """The exit code of a swarm command run by the named agent of swarm sw."""
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    return cli.main(["sw", *argv])


@pytest.mark.parametrize("argv", [("block", "stuck"), ("restack",), ("progress", "--doing", "x", "--ends-when", "y")])
def test_the_master_is_told_it_works_no_task(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    assert ran(monkeypatch, MASTER, *argv) == 1
    assert capsys.readouterr().err == f"swarm: {MASTER} is the swarm master; the master works no task\n"


@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_a_worker_passes_its_task_gates_before_a_merge_queue_step(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    judged: list[str] = []
    monkeypatch.setattr(cli.merge_queue, "operate", lambda asked, url, **options: {"action": asked, "url": url})
    monkeypatch.setattr(cli.done_gate, "require_local", lambda held, slug, task: judged.append(f"local {task}"))
    monkeypatch.setattr(cli, "_require_target", lambda held, args, agent: judged.append(f"target {agent.task}"))
    assert ran(monkeypatch, ENGINEER, "merge", action, PULL) == 0
    assert judged == ["local t1", "target t1"]
    assert capsys.readouterr().out == f'{{"action": "{action}", "url": "{PULL}"}}\n'
