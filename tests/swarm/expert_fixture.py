"""What the expert lineage tests share: a swarm store holding a master and an engineer beside a stranger's swarm, a
mission file, who acts, a started lineage, a seated expert and the swarm command."""

import hashlib
from pathlib import Path

import pytest

from scripts.swarm import cli, expert_lineage
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_expert_mission import GOOD

MASTER, ENGINEER, STRANGER = "master@a1b2c3-0001", "engineer@a1b2c3-0002", "master@d4e5f6-0001"
EXPERT, SECOND_EXPERT = "expert@a1b2c3-0001", "expert@a1b2c3-0002"
DIGEST = hashlib.sha256(GOOD.encode()).hexdigest()
ADDRESS = f"mission:sw/{DIGEST}"
TITLE = "Keep an ordered batch built"
CONTROLS = "only the operator or the master of swarm sw uses its swarm controls"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RedisStore:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)
    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", state="running", max_eng=1, max_ci=0))
    found.create(SwarmConfig("other", "/repo", 1, 0))
    found.put_agent("sw", AgentRecord(MASTER, "master", "master", seat="master@sw"))
    found.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1"))
    found.put_agent("other", AgentRecord(STRANGER, "master", "master", seat="master@other"))
    monkeypatch.setattr(cli, "connect", lambda: found)
    return found


@pytest.fixture
def mission_file(tmp_path: Path) -> Path:
    path = tmp_path / "missions" / "batch.md"
    path.parent.mkdir()
    path.write_text(GOOD, encoding="utf-8")
    return path


def acting(monkeypatch: pytest.MonkeyPatch, name: str, swarm: str = "sw") -> None:
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)


def operating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Act as the operator: a session that carries no agent name and no swarm."""
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)


def started(store: RedisStore, effort: str = "xhigh", by: str = MASTER, at: int = 1_000) -> expert_lineage.Lineage:
    return expert_lineage.start(store, "sw", GOOD, "/missions/batch.md", effort, by, at)


def seated(store: RedisStore, name: str, lineage: str, state: str = "working") -> None:
    store.put_agent("sw", AgentRecord(name, "expert", lineage, seat=f"{lineage}@sw", state=state))


def run(*argv: str) -> int:
    return cli.main(list(argv))


def stored(store: RedisStore, part: str) -> dict[str, str]:
    found: dict[str, str] = store.redis.hgetall(store.key("sw", part))
    return found


def indexed(store: RedisStore, slug: str = "sw") -> set[str]:
    """The ids the swarm indexes as running lineages."""
    found: set[str] = store.redis.smembers(store.key(slug, "expert-running"))
    return found
