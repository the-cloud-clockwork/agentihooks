import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server  # noqa: E402
import new_ledger  # noqa: E402

from scripts.inbox import seen  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "relay-outage-2026-01-01"
ENG = AgentRecord("ro-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("ro-master-1", MASTER, MASTER, pane_id="pm", seat=f"master@{SLUG}")
LONG = " ".join(["word"] * 120)


class Outage:
    """One fake Redis server the ledger server connects to, refused while it is down."""

    def __init__(self, monkeypatch):
        import fakeredis

        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        store = RedisStore(self.redis)
        store.create(SwarmConfig(SLUG, "/repo", 1, 0))
        for agent in (ENG, BOSS):
            store.put_agent(SLUG, agent)
            store.seats.occupy(agent.seat, agent.name, 1)
        monkeypatch.setattr("scripts.swarm.store.redis_client", self.client)
        monkeypatch.setattr(seen, "marks_for", lambda slug, environ=None: seen.SeenMarks(self.redis))

    def client(self, environ=None):
        import fakeredis

        client = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        client.ping()
        return client

    def down(self, value=True):
        self.server.connected = not value

    def texts(self, address):
        return [item.text for item in InboxStore(self.redis).inbox(address)]


@pytest.fixture
def outage(monkeypatch):
    return Outage(monkeypatch)


def make_ledger(description="d"):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": description}],
        "questions": [{"text": "which broker?"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    return state["phases"][0]["id"], f"questions/{state['questions'][0]['id']}"


def write(op, refused=()):
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == list(refused)
    ledger_server.relay_to_inbox(SLUG, state)
    ledger_server.deliver_alerts(SLUG, state)
    return state


def operator_writes(phase, question):
    write({"op": "add", "thread": "chat", "id": "m-1", "text": "@ro-eng-1 please rebase onto dev"})
    write({"op": "add", "thread": f"phases/{phase}/comments", "id": "c-1", "text": "the phase needs a doc"})
    write({"op": "answer", "id": "an-1", "item": question, "text": "use the queue"})


def mentions(texts, words):
    return [text for text in texts if words in text]


def assert_each_write_reached_its_agent_once(outage):
    eng, boss = outage.texts(ENG.seat), outage.texts(BOSS.seat)
    assert len(mentions(eng, "please rebase onto dev")) == 1
    assert len(mentions(boss, "the phase needs a doc")) == 1
    assert len(mentions(boss, "use the queue")) == 1
    assert mentions(boss, "please rebase onto dev") == [] and mentions(eng, "use the queue") == []


def test_writes_made_while_redis_is_down_reach_their_agents_once_after_recovery(outage):
    phase, question = make_ledger()
    outage.down()
    operator_writes(phase, question)
    outage.down(False)
    ledger_server.replay_unrelayed()
    ledger_server.replay_unrelayed()
    assert_each_write_reached_its_agent_once(outage)


def test_the_next_write_after_recovery_replays_every_revision_it_could_not_relay(outage):
    phase, question = make_ledger()
    outage.down()
    operator_writes(phase, question)
    outage.down(False)
    write({"op": "add", "thread": "chat", "id": "m-2", "text": "@ro-eng-1 and run the docs build"})
    ledger_server.replay_unrelayed()
    assert_each_write_reached_its_agent_once(outage)
    assert len(mentions(outage.texts(ENG.seat), "run the docs build")) == 1


def test_a_write_relayed_while_redis_is_up_leaves_nothing_to_replay(outage):
    phase, question = make_ledger()
    operator_writes(phase, question)
    assert ledger_server.unrelayed() == {}
    ledger_server.replay_unrelayed()
    assert_each_write_reached_its_agent_once(outage)


def test_an_alert_raised_while_redis_is_down_reaches_the_master_once_after_recovery(outage):
    make_ledger()
    outage.down()
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    write(taken, refused=["plan"])
    outage.down(False)
    ledger_server.replay_unrelayed()
    ledger_server.replay_unrelayed()
    assert len([text for text in outage.texts(BOSS.seat) if "alert" in text]) == 1
