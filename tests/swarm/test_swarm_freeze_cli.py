import json

import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeLedger

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

MASTER, ENGINEER = "master@c4d5e6-0001", "engineer@c4d5e6-0002"
DISPATCHER, CI, PLANNER = "dispatcher@c4d5e6-0003", "ci@c4d5e6-0004", "planner@c4d5e6-0005"


@pytest.fixture
def swarm(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", state="running", max_eng=2, max_ci=1))
    store.put_agent("demo", AgentRecord(MASTER, "master", "master", seat="master@demo"))
    store.put_agent("demo", AgentRecord(ENGINEER, "eng", "t1"))
    store.put_agent("demo", AgentRecord(DISPATCHER, "dispatch", "dispatcher", seat="dispatcher@demo"))
    store.put_agent("demo", AgentRecord(CI, "ci", "t2"))
    store.put_agent("demo", AgentRecord(PLANNER, "plan", "t3"))
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
    assert capsys.readouterr().err == (
        "swarm: the master writes freezes only with the operator's words: pass them with --quote\n"
    )


def test_an_engineers_freeze_command_is_refused(swarm, monkeypatch, capsys):
    _, ledger = swarm
    acting(monkeypatch, ENGINEER, "demo")
    assert cli.main(["demo", "freeze", "plans/a", "--quote", "freeze it"]) == 1
    assert ledger.freezes == []
    assert "is neither" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_the_dispatcher_at_full_autonomy_writes_each_verb_as_the_dispatcher(swarm, monkeypatch, capsys, verb):
    store, ledger = swarm
    store.update("demo", autonomy="full")
    acting(monkeypatch, DISPATCHER, "demo")
    assert cli.main(["demo", verb, "plans/a", "--reason", "ship first"]) == 0
    assert ledger.freezes == [("demo", verb, "plans/a", {"by": "dispatcher", "reason": "ship first", "quote": ""})]
    assert json.loads(capsys.readouterr().out) == {verb: "plans/a", "by": "dispatcher"}


@pytest.mark.parametrize("autonomy", ["manual", "assist", "delegate"])
@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_the_dispatcher_below_full_autonomy_is_refused(swarm, monkeypatch, capsys, autonomy, verb):
    store, ledger = swarm
    store.update("demo", autonomy=autonomy)
    acting(monkeypatch, DISPATCHER, "demo")
    assert cli.main(["demo", verb, "plans/a", "--quote", "freeze it"]) == 1
    assert ledger.freezes == []
    assert capsys.readouterr().err == (
        f"swarm: the dispatcher of swarm demo writes freezes only at full autonomy, and its autonomy is {autonomy}\n"
    )


def test_a_finished_dispatcher_is_refused(swarm, monkeypatch, capsys):
    store, ledger = swarm
    store.update("demo", autonomy="full")
    store.put_agent("demo", AgentRecord(DISPATCHER, "dispatch", "dispatcher", state="finished"))
    acting(monkeypatch, DISPATCHER, "demo")
    assert cli.main(["demo", "focus", "plans/a"]) == 1
    assert ledger.freezes == []
    assert capsys.readouterr().err == (
        f"swarm: only the operator or the master of swarm demo uses its swarm controls, and {DISPATCHER} is neither\n"
    )


def test_the_dispatcher_of_another_swarm_is_refused(swarm, monkeypatch, capsys):
    store, ledger = swarm
    store.update("demo", autonomy="full")
    acting(monkeypatch, DISPATCHER, "other")
    assert cli.main(["demo", "focus", "plans/a"]) == 1
    assert ledger.freezes == []
    assert capsys.readouterr().err == (
        f"swarm: only the operator or the master of swarm demo uses its swarm controls, and {DISPATCHER} is neither\n"
    )


@pytest.mark.parametrize("name", [ENGINEER, CI, PLANNER])
@pytest.mark.parametrize("verb", ["freeze", "focus", "unfreeze"])
def test_every_lane_agent_at_full_autonomy_is_still_refused(swarm, monkeypatch, capsys, name, verb):
    store, ledger = swarm
    store.update("demo", autonomy="full")
    acting(monkeypatch, name, "demo")
    assert cli.main(["demo", verb, "plans/a", "--quote", "freeze it"]) == 1
    assert ledger.freezes == []
    assert capsys.readouterr().err == (
        f"swarm: only the operator or the master of swarm demo uses its swarm controls, and {name} is neither\n"
    )


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
        ("unfreeze", None, {"reason": "done"}, {"op": "freeze_clear", "target": "plans/a", "reason": "done"}),
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


def test_status_lists_each_active_freeze(env, capsys):  # noqa: F811
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    state = ledger.state
    ledger.state = lambda slug: {
        **state(slug),
        "freezes": [{"id": "f1", "verb": "freeze", "target": "lane:ci", "by": "operator", "at": 0, "reason": "hold"}],
    }
    capsys.readouterr()
    run("sw", "status")
    assert "freeze  lane:ci  by operator  at 1970-01-01T00:00Z  hold" in capsys.readouterr().out.splitlines()
