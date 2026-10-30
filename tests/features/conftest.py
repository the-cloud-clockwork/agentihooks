"""Feature suite fixtures: a swarm on the real Redis service, a ledger on the real Postgres service served by a
ledger server subprocess, and scripted agents launched as their own processes with the real launch env.

The suite runs only with AGENTIHOOKS_FEATURE_SUITE=1 and without the unit suite's conftest, which refuses Redis:
python -m pytest tests/features --confcutdir=tests/features
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.profiles import binding
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, connect
from scripts.swarm_ledger import ledger_server, storage
from scripts.swarm_ledger.repository import postgres
from tests.features import scripted_agent

ENABLED = "AGENTIHOOKS_FEATURE_SUITE"
REDIS = "AGENTIHOOKS_SWARM_REDIS_URL"
FOLDER = Path(__file__).resolve().parent
ROOT = FOLDER.parents[1]
SERVER_WAIT_S = 30
PROFILE = "engineer"
LANE = "eng"
TASK = "t1"
CONTENT = {
    "title": "Feature suite ledger",
    "overview": "Prove swarm features end to end on real services.",
    "sources": [],
    "phases": [{"title": "Features", "description": "Every feature runs on the services."}],
    "tasks": [{"title": "Scripted agent task", "phase": "p1", "lane": LANE}],
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the suite unless its service containers are declared."""
    if os.environ.get(ENABLED) == "1":
        return
    skip = pytest.mark.skip(reason=f"the feature suite runs only with {ENABLED}=1 against its service containers")
    for item in items:
        if FOLDER in Path(item.path).parents:
            item.add_marker(skip)


def free_port() -> int:
    """A spare local TCP port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass(frozen=True)
class Ledger:
    slug: str
    folder: Path
    env: dict[str, str]


@dataclass(frozen=True)
class Swarm:
    store: RedisStore
    ledger: Ledger


@dataclass
class Agent:
    swarm: Swarm
    record: AgentRecord
    process: subprocess.Popen
    log: Path

    def hand_off(self, doc: Path, timeout: float = 120) -> dict:
        """Ask the agent to hand off its seat with the document given and return its command result."""
        redis = self.swarm.store.redis
        slug = self.swarm.ledger.slug
        redis.set(scripted_agent.handoff_key(slug, self.record.name), str(doc))
        deadline = time.monotonic() + timeout
        while (raw := redis.get(scripted_agent.result_key(slug, self.record.name))) is None:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.record.name} did not hand off within {timeout:g}s")
            time.sleep(0.2)
        return json.loads(raw)


def _environment(folder: Path, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "LEDGER_DIR": str(folder),
        "LEDGER_PORT": str(port),
        "LEDGER_AUTOSTART": "0",
        "SWARM_RELOAD": "0",
        "AGENTIHOOKS_DEPLOYMENT": "local",
        "AGENTIHOOKS_SERENA": "false",
        "PYTHONPATH": str(ROOT),
    }


def _listening(server: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + SERVER_WAIT_S
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"ledger server exited with {server.returncode} before listening")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"ledger server did not listen on port {port} within {SERVER_WAIT_S}s")


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    """A seeded ledger in the Postgres service, served by a ledger server subprocess on a spare port."""
    assert postgres.url(os.environ), f"{postgres.URL} must name the Postgres service"
    slug = f"feature-{uuid.uuid4().hex[:10]}"
    folder = tmp_path / "ledger"
    folder.mkdir()
    assert storage.build().create(slug, CONTENT)
    port = free_port()
    env = _environment(folder, port)
    with (tmp_path / "ledger-server.log").open("w") as log:
        server = subprocess.Popen(
            [sys.executable, ledger_server.__file__, "--serve"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        try:
            _listening(server, port)
            yield Ledger(slug, folder, env)
        finally:
            _stop(server)


@pytest.fixture
def swarm(ledger: Ledger) -> Swarm:
    """A running swarm in the Redis service named after the ledger."""
    assert os.environ.get(REDIS), f"{REDIS} must name the Redis service"
    store = connect()
    store.create(SwarmConfig(ledger.slug, str(ROOT), 1, 0, lanes={LANE: {"agent": "claude", "profile": PROFILE}}))
    return Swarm(store, ledger)


def _home(root: Path) -> Path:
    home = root / PROFILE / scripted_agent.HARNESS
    home.mkdir(parents=True)
    (home / binding.PERSONAS[scripted_agent.HARNESS]).write_text(binding.persona("# Feature suite engineer\n"))
    (home.parent / f"{scripted_agent.HARNESS}.sources.json").write_text("[]\n")
    binding.write(home, PROFILE, scripted_agent.HARNESS)
    return home


@pytest.fixture
def scripted_agent_launch(swarm: Swarm, tmp_path: Path) -> Iterator[Callable[..., Agent]]:
    """Launch scripted agents into the swarm's task seat the way the swarm launches a real one."""
    launched: list[Agent] = []

    def launch(canary_delay_s: float = 0.0, beat_s: float = 0.5) -> Agent:
        store, slug = swarm.store, swarm.ledger.slug
        root = tmp_path / f"agent-{len(launched)}"
        home = _home(root / "homes")
        report = root / "profile-report.json"
        binding.request(report, PROFILE, scripted_agent.HARNESS, home)
        name = store.next_name(slug, LANE)
        record = AgentRecord(
            name,
            LANE,
            TASK,
            seat=f"{LANE}-1@{slug}",
            state="starting",
            profile=PROFILE,
            harness=scripted_agent.HARNESS,
            profile_decision={"validation": {"state": "pending", "report": str(report), "account": ""}},
        )
        store.put_agent(slug, record)
        store.seats.occupy(record.seat, name, lease.now_ms(store))
        env = {
            **swarm.ledger.env,
            binding.REPORT: str(report),
            binding.HOMES[scripted_agent.HARNESS]: str(home),
            "AGENTIHOOKS_PROFILE": PROFILE,
            "AGENTIHOOKS_SWARM": slug,
            "AGENTIHOOKS_SWARM_LANE": LANE,
            "AGENTIHOOKS_SWARM_TASK": TASK,
            "AGENTIHOOKS_AGENT_NAME": name,
            scripted_agent.CANARY_DELAY: str(canary_delay_s),
            scripted_agent.BEAT_EVERY: str(beat_s),
        }
        log = root / "agent.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.features.scripted_agent"],
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        agent = Agent(swarm, record, process, log)
        launched.append(agent)
        return agent

    yield launch
    for agent in launched:
        if agent.process.poll() is None:
            _stop(agent.process)
