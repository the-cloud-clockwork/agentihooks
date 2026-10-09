import json
from dataclasses import asdict

import pytest

from scripts.swarm import cli, prompt, templates
from scripts.swarm.health.findings import _is_worker
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from scripts.swarm_ledger import ledger_server, ledger_tasks
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", 1, 1, state="paused"))
    return found


def test_old_config_defaults_and_zero_cap_round_trips(store):
    store.redis.hdel(store.key("sw", "config"), "max_plan")
    assert store.config("sw").max_plan == 1
    assert store.update("sw", max_plan=0).max_plan == 0
    assert store.config("sw").max_plan == 0


def test_create_set_list_status_and_saved_template_carry_the_cap(store, monkeypatch, capsys):
    ledger = FakeLedger([])
    ledger.said = []
    ledger.say = lambda slug, text, by=None: ledger.said.append(text)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli.main(["new", "create", "--repo", "/repo", "--max-plan-agents", "3"]) == 0
    assert store.config("new").max_plan == 3
    assert cli.main(["sw", "set", "max-plan-agents=2"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["max_plan"] == 2
    assert templates.from_config("saved", store.config("sw")).lanes["plan"].cap == 2
    assert cli.main(["list"]) == 0
    assert "plan 2" in capsys.readouterr().out
    assert cli.main(["sw", "status"]) == 0
    assert "plan 2" in capsys.readouterr().out
    assert cli.main(["sw", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["config"]["max_plan"] == 2


def test_tick_uses_independent_planner_cap_and_seat(store):
    store.update("sw", state="running")
    ledger = FakeLedger(
        [
            {"id": "engineer", "lane": "eng"},
            {"id": "ci", "lane": "ci"},
            {"id": "plan-one", "lane": "plan"},
            {"id": "plan-two", "lane": "plan"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    assert [lane for lane, _, _ in runtime.spawned] == ["eng", "ci", "plan"]
    planner = next(a for a in store.agents("sw") if a.lane == "plan")
    assert planner.name.startswith("planner@")
    assert planner.seat == "plan-1@sw"
    assert store.seats.occupant(planner.seat).occupant == planner.name
    tick("sw", store, ledger, runtime, 2)
    assert len(runtime.spawned) == 3
    store.update("sw", max_plan=2)
    tick("sw", store, ledger, runtime, 3)
    assert runtime.spawned[-1][0] == "plan"
    assert next(a for a in store.agents("sw") if a.task == "plan-two").seat == "plan-2@sw"


def test_zero_planner_cap_keeps_task_open_and_drained_swarm_wakes(store):
    store.update("sw", state="running", max_plan=0)
    ledger, runtime = FakeLedger([{"id": "slice", "lane": "plan"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    assert runtime.spawned == []
    assert ledger.rows["slice"]["state"] == "open"
    store.update("sw", state="drained", max_plan=1)
    tick("sw", store, ledger, runtime, 2)
    assert [lane for lane, _, _ in runtime.spawned] == ["plan"]
    assert store.config("sw").state == "running"


def test_ledger_accepts_planner_lane_and_prompt_names_role():
    ledger_tasks.check_task({"id": "slice", "lane": "plan", "kind": "plan"})
    text = prompt.build("sw", "/repo", "plan", "planner@abcdef-0001", {"id": "slice", "title": "Slice"})
    assert "a planner" in text
    assert not _is_worker("planner@abcdef-0001")
    assert _is_worker("engineer@abcdef-0001")
    assert _is_worker("ci@abcdef-0001")


@pytest.mark.parametrize("cap", [0, 2, 50])
def test_page_control_sets_planner_cap(cap):
    assert ledger_server.control_argv({"action": "set", "max_plan": cap}) == ["set", f"max-plan-agents={cap}"]


@pytest.mark.parametrize("cap", [-1, 51, True, "2"])
def test_page_refuses_invalid_planner_cap(cap):
    with pytest.raises(ValueError, match="max_plan"):
        ledger_server.control_argv({"action": "set", "max_plan": cap})


def test_templates_keep_planner_profile_and_cap():
    config = SwarmConfig("sw", "/repo", 1, 1)
    template = templates.from_config("saved", config)
    assert asdict(template.lanes["plan"])["profile"] == "planner"
    assert template.lanes["plan"].cap == 1


def test_empty_page_set_names_all_cap_fields():
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set"})
    assert (
        str(caught.value) == "set needs max_eng, max_ci, max_plan, compact_limit, effort_min, effort_max, autonomy, "
        "scaling, load_high, load_low, memory_per_agent_mb, master_agent, overlays or gates"
    )


@pytest.mark.parametrize("limit", [100, 650, 1000])
def test_page_control_sets_the_compact_limit(limit):
    assert ledger_server.control_argv({"action": "set", "compact_limit": limit}) == ["set", f"compact-limit={limit}"]


@pytest.mark.parametrize("limit", [0, 99, 1001])
def test_page_refuses_a_compact_limit_outside_100_to_1000(limit):
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", "compact_limit": limit})
    assert str(caught.value) == "compact_limit must be a whole number from 100 to 1000"


@pytest.mark.parametrize("mode", ["manual", "assist", "delegate", "full"])
def test_page_control_sets_each_autonomy_mode(mode):
    assert ledger_server.control_argv({"action": "set", "autonomy": mode}) == ["set", f"autonomy={mode}"]


def test_page_refuses_an_unknown_autonomy_mode():
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", "autonomy": "auto"})
    assert str(caught.value) == "autonomy must be one of manual, assist, delegate, full"


def test_create_parser_defaults_to_template_and_parses_integer_cap():
    parser = cli.build_parser()
    assert parser.parse_args(["sw", "create", "--repo", "/repo"]).max_plan_agents is None
    assert parser.parse_args(["sw", "create", "--repo", "/repo", "--max-plan-agents", "0"]).max_plan_agents == 0
    with pytest.raises(SystemExit):
        parser.parse_args(["sw", "create", "--repo", "/repo", "--max-plan-agents", "invalid"])
