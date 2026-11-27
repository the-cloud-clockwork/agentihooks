"""A runtime retire that ends no process refuses while a live process still carries the agent name."""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import fakeredis
import pytest

from scripts.swarm import naming, reaper, retire_watch, tick
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.steps import agents
from tests.swarm.test_tick import FakeRuntime, tasks

SLEEP = "import time; time.sleep(300)"
ROOT = Path(__file__).resolve().parents[2]
CALLER = """
import subprocess
import sys
from pathlib import Path

from scripts.swarm import naming
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord

naming.resolve_name = lambda name: name
sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
try:
    runtime = HerdrRuntime(home=Path(sys.argv[1]), herdr=lambda args: {}, choose=lambda *_: ("claude", "open"))
    print(runtime.retire(AgentRecord(sys.argv[2], "eng", "t1")))
finally:
    sibling.kill()
    sibling.wait()
"""
Plant = Callable[..., subprocess.Popen[bytes]]


@pytest.fixture(autouse=True)
def plain_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(naming, "resolve_name", lambda name: name)


@pytest.fixture
def plant() -> Iterator[Plant]:
    planted: list[subprocess.Popen[bytes]] = []

    def start(
        argv: tuple[str, ...] = (), env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> subprocess.Popen[bytes]:
        environ = {key: value for key, value in os.environ.items() if key != reaper.NAME_KEY}
        proc = subprocess.Popen(
            [sys.executable, "-c", SLEEP, *argv], env={**environ, **(env or {})}, cwd=cwd, start_new_session=True
        )
        planted.append(proc)
        return proc

    yield start
    for proc in planted:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _name(role: str = "engineer") -> str:
    return f"{role}@{uuid.uuid4().hex[:6]}-0001"


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HerdrRuntime:
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    return HerdrRuntime(home=tmp_path, herdr=lambda args: {}, choose=lambda *_: ("claude", "open"))


def _refused(pid: int, name: str) -> dict[str, Any]:
    return {"process": pid, "refusal": f"process {pid} still carries {name}"}


class _LiveRetire(FakeRuntime):
    def __init__(self, herdr: HerdrRuntime) -> None:
        super().__init__()
        self.herdr = herdr

    def retire(self, agent: AgentRecord, homes: Sequence[Path] = ()) -> bool:
        return self.herdr.retire(agent, homes)

    def refusal(self, agent: AgentRecord) -> dict[str, Any]:
        return self.herdr.refusal(agent)


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_a_retire_that_ends_nothing_refuses_while_a_process_carries_the_name_in_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Plant
) -> None:
    runtime, name = _runtime(tmp_path, monkeypatch), _name()
    carrier = plant(env={reaper.NAME_KEY: name})
    agent = AgentRecord(name, "eng", "t1")

    assert runtime.retire(agent) is False
    assert runtime.refusal(agent) == _refused(carrier.pid, name)
    assert carrier.poll() is None


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_a_retire_that_ends_nothing_refuses_while_a_process_carries_the_name_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Plant
) -> None:
    runtime, name = _runtime(tmp_path, monkeypatch), _name()
    carrier = plant(argv=("--name", name))
    agent = AgentRecord(name, "eng", "t1")

    assert runtime.retire_process(agent, None) is False
    assert runtime.refusal(agent) == _refused(carrier.pid, name)
    assert carrier.poll() is None


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_the_tick_keeps_a_lost_agent_and_its_seat_while_a_process_carries_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Plant
) -> None:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    ledger: Any = tasks(("t1", "eng"))
    runtime: Any = _LiveRetire(_runtime(tmp_path, monkeypatch))
    master = AgentRecord(_name("master"), "master", "master", started_at=1, seat="master@sw")
    store.put_agent("sw", master)
    carrier = plant(env={reaper.NAME_KEY: master.name})
    monkeypatch.setattr(tick.reaper, "scratch_homes", lambda slug, task: [])
    monkeypatch.setattr(tick.dead_resume, "reopen", Mock(return_value=False))
    at = tick.STARTUP_GRACE_MS + 2

    actions = agents._reap("sw", store, ledger, runtime, ledger.rows, at)

    assert actions == [f"could not retire {master.name}, retrying next tick"]
    assert store.agents("sw") == [master]
    assert retire_watch.rows(store, "sw") == [
        {"agent": master.name, "ticks": 1, "since": at, "at": at, **_refused(carrier.pid, master.name)}
    ]
    assert carrier.poll() is None


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_a_retire_that_ends_nothing_succeeds_when_no_process_carries_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    agent = AgentRecord(_name(), "eng", "t1")

    assert runtime.retire(agent) is True
    assert runtime.refusal(agent) == {"process": 0, "refusal": "unknown"}


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_a_carrier_that_ended_after_the_pass_snapshot_no_longer_holds_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Plant
) -> None:
    runtime, name = _runtime(tmp_path, monkeypatch), _name()
    carrier = plant(argv=("--name", name))
    agent = AgentRecord(name, "eng", "t1")

    @reaper.scanning_pass
    def retire_after_the_carrier_ends() -> bool:
        reaper.targets(name, None, [])
        carrier.kill()
        carrier.wait()
        return runtime.retire(agent)

    assert retire_after_the_carrier_ends() is True


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_an_agent_retiring_its_own_name_is_not_held_by_its_own_processes(tmp_path: Path) -> None:
    name = _name("master")
    environ = {**os.environ, reaper.NAME_KEY: name, "PYTHONPATH": str(ROOT)}
    environ.pop("AGENTIHOOKS_SWARM", None)

    result = subprocess.run(
        [sys.executable, "-c", CALLER, str(tmp_path), name],
        env=environ,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.stdout.strip() == "True", result.stderr


@pytest.mark.feature("runtime-retire-refuses-live-named-process")
def test_a_retire_that_ended_the_scratch_home_carrier_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: Plant
) -> None:
    runtime, name = _runtime(tmp_path, monkeypatch), _name()
    home = (tmp_path / "scratch").resolve()
    home.mkdir()
    carrier = plant(env={reaper.NAME_KEY: name}, cwd=home)
    stray = plant(env={reaper.NAME_KEY: name})
    agent = AgentRecord(name, "eng", "t1")

    assert runtime.retire(agent, homes=[home]) is True
    assert carrier.wait(timeout=10) is not None
    assert stray.poll() is None
    assert runtime.refusal(agent) == {"process": 0, "refusal": "unknown"}
