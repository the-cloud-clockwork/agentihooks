import json

import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

MASTER, ENGINEER = "master@c4d5e6-0001", "engineer@c4d5e6-0002"


@pytest.fixture
def swarm(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", state="running", max_eng=2, max_ci=1))
    store.put_agent("demo", AgentRecord(MASTER, "master", "master", seat="master@demo"))
    store.put_agent("demo", AgentRecord(ENGINEER, "eng", "t1"))
    ledger = FakeLedger([])
    ledger.freezes = []
    ledger.freeze = lambda slug, verb, target, **fields: ledger.freezes.append((slug, verb, target, fields))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    return store, ledger


def acting(monkeypatch, name="", swarm=""):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)


@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_the_operator_writes_each_verb_without_an_author(swarm, monkeypatch, capsys, verb):
    _, ledger = swarm
    acting(monkeypatch)
    assert cli.main(["demo", verb, "plans/a", "--reason", "ship first"]) == 0
    assert ledger.freezes == [("demo", verb, "plans/a", {"by": None, "reason": "ship first", "quote": ""})]
    assert json.loads(capsys.readouterr().out) == {verb: "plans/a", "by": "operator"}


def test_the_master_writes_with_the_operators_words(swarm, monkeypatch):
    _, ledger = swarm
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", "freeze", "lane:ci", "--quote", "freeze the ci lane"]) == 0
    assert ledger.freezes == [
        ("demo", "freeze", "lane:ci", {"by": MASTER, "reason": "", "quote": "freeze the ci lane"})
    ]


def test_the_master_without_the_operators_words_is_refused(swarm, monkeypatch, capsys):
    _, ledger = swarm
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", "focus", "plans/a"]) == 1
    assert ledger.freezes == []
    assert "the master writes freezes only with the operator's words" in capsys.readouterr().err


def test_an_engineers_freeze_command_is_refused(swarm, monkeypatch, capsys):
    _, ledger = swarm
    acting(monkeypatch, ENGINEER, "demo")
    assert cli.main(["demo", "freeze", "plans/a", "--quote", "freeze it"]) == 1
    assert ledger.freezes == []
    assert "is neither" in capsys.readouterr().err


@pytest.mark.parametrize(
    "verb, by, fields, op",
    [
        ("freeze", None, {}, {"op": "freeze_set", "verb": "freeze", "target": "plans/a"}),
        (
            "focus",
            MASTER,
            {"reason": "ship", "quote": "focus on a"},
            {
                "op": "freeze_set",
                "verb": "focus",
                "target": "plans/a",
                "by": MASTER,
                "reason": "ship",
                "quote": "focus on a",
            },
        ),
        ("unfreeze", "dispatcher", {}, {"op": "freeze_clear", "target": "plans/a", "by": "dispatcher"}),
    ],
)
def test_the_client_sends_one_freeze_op(monkeypatch, verb, by, fields, op):
    sent = []
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops: sent.append((slug, ops)))
    LedgerClient().freeze("demo", verb, "plans/a", by=by, **fields)
    [(slug, [written])] = sent
    assert slug == "demo"
    assert written.pop("id").startswith(op["op"] + "-")
    assert written == op
