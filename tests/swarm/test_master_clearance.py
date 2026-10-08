import re
import sys
from pathlib import Path

import pytest

from scripts.gates import catalog, entry, lift, modes
from scripts.swarm import clearance, cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

MASTER, ENGINEER, PLANNER, STRANGER = (
    "master@a1b2c3-0001",
    "engineer@a1b2c3-0002",
    "planner@a1b2c3-0003",
    "master@d4e5f6-0001",
)
SHELL = Path(ledger_server.__file__).with_name("shell.html")


@pytest.fixture
def swarm(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", state="running", max_eng=2, max_ci=1))
    store.create(SwarmConfig("other", "/repo", 1, 0))
    store.put_agent("demo", AgentRecord(MASTER, "master", "master", seat="master@demo"))
    store.put_agent("demo", AgentRecord(ENGINEER, "eng", "t1"))
    store.put_agent("demo", AgentRecord(PLANNER, "plan", "p1"))
    store.put_agent("other", AgentRecord(STRANGER, "master", "master", seat="master@other"))
    ledger = FakeLedger([])
    ledger.said = []
    ledger.say = lambda slug, text, by=None: ledger.said.append((text, by) if slug == "demo" else (slug, text, by))
    ledger.notes = []
    ledger.notify = lambda slug, text: ledger.notes.append(text if slug == "demo" else (slug, text))
    ledger.summarize = ledger.mark_closed = ledger.reopen = lambda *args: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "run_tick", lambda *args: [])
    monkeypatch.setattr(cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(cli.timer, "ensure", lambda *args: True)
    monkeypatch.setattr(cli.plan_shape, "report", lambda tasks, cap: {"summary": "", "warning": ""})
    return store, ledger


def acting(monkeypatch, name, swarm):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)


FAMILIES = {
    "lifecycle": (["pause"], "master a1b2c3 0001 changed the swarm state with pause from running to paused."),
    "capacity": (
        ["set", "max-eng-agents=5", "effort-max=max", "master-agent=codex"],
        "master a1b2c3 0001 changed max eng agents from 2 to 5, effort max from high to max, "
        "master agent from claude to codex.",
    ),
    "autonomy": (["set", "autonomy=full"], "master a1b2c3 0001 changed autonomy from delegate to full."),
    "gate mode": (["set", "intent-gate=coach"], "master a1b2c3 0001 changed intent gate from log only to coach."),
    "gate lift": (
        ["lift", ENGINEER, "watch"],
        "master a1b2c3 0001 changed watch gate lift for engineer a1b2c3 0002 from not lifted to lifted.",
    ),
}


def changed(store):
    config = store.config("demo")
    return (config.state, config.max_eng, config.autonomy, config.gates, lift.agent_lifted("demo", ENGINEER, "watch"))


@pytest.mark.parametrize("family", FAMILIES)
def test_the_master_of_the_swarm_uses_each_control_and_the_ledger_records_it(swarm, monkeypatch, family):
    store, ledger = swarm
    argv, record = FAMILIES[family]
    store.update("demo", lanes={"master": {"agent": "claude"}})
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", *argv]) == 0
    assert ledger.notes == [record]


@pytest.mark.parametrize(
    "name,swarm_of", [(ENGINEER, "demo"), (PLANNER, "demo"), (STRANGER, "other"), (STRANGER, ""), ("worker-2", "")]
)
@pytest.mark.parametrize("family", FAMILIES)
def test_an_agent_or_another_swarms_master_is_refused(swarm, monkeypatch, capsys, family, name, swarm_of):
    store, ledger = swarm
    before = changed(store)
    acting(monkeypatch, name, swarm_of)
    assert cli.main(["demo", *FAMILIES[family][0]]) == 1
    assert capsys.readouterr().err == refused(name)
    assert changed(store) == before
    assert ledger.notes == []


@pytest.mark.parametrize("family", FAMILIES)
def test_the_operator_keeps_every_control_without_a_master_record(swarm, monkeypatch, family):
    store, ledger = swarm
    before = changed(store)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    assert cli.main(["demo", *FAMILIES[family][0]]) == 0
    assert changed(store) != before
    assert [text for text in ledger.notes if text.startswith("master a1b2c3 0001")] == []


def test_a_hand_taken_master_without_a_swarm_pin_keeps_its_clearance(swarm, monkeypatch):
    store, ledger = swarm
    acting(monkeypatch, MASTER, "")
    assert cli.main(["demo", "pause"]) == 0
    assert store.config("demo").state == "paused"
    assert ledger.notes == ["master a1b2c3 0001 changed the swarm state with pause from running to paused."]


def test_a_master_use_that_changes_nothing_is_still_recorded(swarm, monkeypatch):
    store, ledger = swarm
    store.update("demo", state="paused")
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", "pause"]) == 0
    assert ledger.notes == ["master a1b2c3 0001 changed the swarm state with pause from paused to paused."]


def test_a_lane_field_records_unset_and_keeps_an_equals_sign_in_its_value(swarm, monkeypatch):
    store, ledger = swarm
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", "set", "eng-role=fix=now"]) == 0
    assert store.config("demo").lanes["eng"]["role"] == "fix=now"
    assert ledger.notes == ["master a1b2c3 0001 changed eng role from unset to fix=now."]


def refused(name):
    return f"swarm: only the operator or the master of swarm demo uses its swarm controls, and {name} is neither\n"


def test_a_finished_master_holds_no_clearance(swarm, monkeypatch, capsys):
    store, _ = swarm
    store.put_agent("demo", AgentRecord(MASTER, "master", "master", state="finished"))
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", "pause"]) == 1
    assert capsys.readouterr().err == refused(MASTER)


@pytest.mark.parametrize("argv", [["pause"], ["--as", MASTER, "pause"], ["set", "intent-gate=coach"]])
def test_an_agent_that_drops_its_swarm_pin_is_still_refused_by_its_session_name(swarm, monkeypatch, capsys, argv):
    store, ledger = swarm
    before = changed(store)
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: ENGINEER)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    assert cli.main(["demo", *argv]) == 1
    assert capsys.readouterr().err == refused(ENGINEER)
    assert changed(store) == before
    assert ledger.notes == []


def test_the_master_stop_now_is_recorded_before_it_retires_the_master(swarm, monkeypatch):
    _, ledger = swarm
    monkeypatch.setattr(cli, "cmd_stop", lambda store, args: sys.exit(143))
    acting(monkeypatch, MASTER, "demo")
    with pytest.raises(SystemExit):
        cli.main(["demo", "stop", "--now"])
    assert ledger.notes == [
        "master a1b2c3 0001 changed the swarm state with stop now from running to stopping."
    ]


@pytest.mark.parametrize("argv,control", [(["stop", "--now"], "stop now"), (["close", "--now"], "close ledger")])
def test_a_master_stop_that_outlives_its_retirement_records_the_settled_state(swarm, monkeypatch, argv, control):
    store, ledger = swarm
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", *argv]) == 0
    assert store.config("demo").state == "stopped"
    assert ledger.notes[:2] == [
        f"master a1b2c3 0001 changed the swarm state with {control} from running to stopping.",
        f"master a1b2c3 0001 changed the swarm state with {control} from stopping to stopped.",
    ]


SAMPLES = {"compact_limit": 200, "effort_min": "low", "effort_max": "max", "master_agent": "codex"}
NOT_SWARM = {"apply", "doctor_start", "doctor_stop"}


def panel_bodies():
    page = SHELL.read_text()
    swarm_buttons = set(re.findall(r'data-swarm="([a-z_]+)"', page))
    for action in sorted(swarm_buttons - NOT_SWARM):
        if not re.search(r"_(?:up|down)$", action):
            yield {"action": action}
    for mode in re.findall(r'data-autonomy="([a-z]+)"', page):
        yield {"action": "set", "autonomy": mode}
    for key in re.findall(r'data-cap="([a-z_]+)"', page):
        yield {"action": "set", key: SAMPLES.get(key, 1)}
    for name in catalog.defaults():
        for mode in modes.supported(name):
            yield {"action": "set", "gates": {name: mode}}
    for gate in sorted({*entry.GATES, *lift.SERVER_GATES}):
        yield {"action": "lift", "agent": ENGINEER, "gate": gate}


def test_the_walk_finds_every_panel_control_family():
    bodies = list(panel_bodies())
    found = {b["action"] for b in bodies} | {key for b in bodies for key in b if key != "action"}
    assert {"start", "pause", "stop", "stop_now", "close", "reopen", "set", "lift"} <= found
    assert {"autonomy", "max_eng", "max_ci", "max_plan", "compact_limit"} <= found
    assert {"effort_min", "effort_max", "master_agent", "gates", "agent"} <= found


@pytest.mark.parametrize("body", list(panel_bodies()), ids=str)
def test_every_panel_control_is_a_cleared_command_the_command_line_takes(swarm, monkeypatch, body):
    store, _ = swarm
    argv = ledger_server.control_argv(body)
    assert argv[0] in clearance.COMMANDS
    acting(monkeypatch, MASTER, "demo")
    assert cli.main(["demo", *argv]) == 0
