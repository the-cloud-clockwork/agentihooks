import importlib.util
import json
import time
from pathlib import Path
from types import ModuleType

import pytest

from scripts.hive import remove
from scripts.inbox.seats import SeatRegistry
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "ci" / "hive-drain-agent.sh"
SLUG = "kind-drain"
TASK = "drain1"
HIVE = "kind-hive"


class Clock:
    def sleep(self, seconds: float) -> None:
        if seconds:
            pytest.fail("the kind drain agent polled again: its roll order never reached the mail it reads")

    def time_ns(self) -> int:
        return time.time_ns()


def _drain_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("DRAIN_SLUG", SLUG)
    monkeypatch.setenv("DRAIN_TASK", TASK)
    monkeypatch.setenv("DRAIN_HANDOFF_DELAY_S", "0")
    script = tmp_path / "kind_drain_agent.py"
    script.write_text(FIXTURE.read_text().split("exec python - <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0])
    spec = importlib.util.spec_from_file_location("kind_drain_agent", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "time", Clock())
    return module


def _seated(tmp_path: Path) -> tuple[RedisStore, AgentRecord]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, str(tmp_path), 0, 0, state="stopped"))
    name = store.next_name(SLUG, "eng", 1)
    seated = AgentRecord(name, "eng", TASK, seat=f"eng-1@{SLUG}", hive=HIVE, state="working", started_at=1)
    return store, store.start_execution(SLUG, seated)


def _handoff(store: RedisStore) -> tuple[str, str, str]:
    envelope = store.handoff_envelope(SLUG, TASK)
    assert envelope is not None
    return envelope["agent"], envelope["reason"], json.loads(store.redis.get(f"kind-drain:{SLUG}:handed"))["hive"]


@pytest.mark.feature("kind-drain-agent-reads-session-mail")
def test_the_kind_drain_agent_records_its_own_recycle_handoff_once_its_seat_holds_the_roll_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drain_agent = _drain_agent(monkeypatch, tmp_path)
    store, agent = _seated(tmp_path)
    SeatRegistry(store.redis).occupy(agent.seat, agent.name, 1)
    InboxStore(store.redis).send(remove.SENDER, agent.seat, remove.ROLL.format(hive=HIVE, slug=SLUG))

    drain_agent.hand_off(store, agent)

    assert _handoff(store) == (agent.name, "recycle", HIVE)
    assert store.handoff_seat(SLUG, TASK) == agent.seat


@pytest.mark.feature("kind-drain-agent-reads-session-mail")
def test_the_kind_drain_agent_sees_the_roll_order_the_hive_drain_sends_to_its_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    drain_agent = _drain_agent(monkeypatch, tmp_path)
    store, agent = _seated(tmp_path)
    remove._order(InboxStore(store.redis), {}, [(SLUG, agent)], remove.ROLL, HIVE)

    drain_agent.hand_off(store, agent)

    assert _handoff(store) == (agent.name, "recycle", HIVE)
