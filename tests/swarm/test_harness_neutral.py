import json
import os
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from scripts import agent_choice
from scripts.profiles import plugins
from scripts.swarm import capacity, master_launch, quota_handoff, take_master, templates
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import MASTER, SwarmConfig
from scripts.swarm.tick import SpawnError
from tests.swarm.profile_fixture import validated

LANES = ("eng", "ci", "plan", MASTER)


def codex(name="cx", sessions=0, cap=6, state="OPEN"):
    return capacity.Account("codex", name, state, sessions, 90, 90, cap)


@pytest.fixture
def codex_only(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("AH_CC_TOKEN_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity, "_claude", lambda environ, now: [])
    monkeypatch.setattr(capacity, "_codex", lambda environ, now, refresh: [codex("cx"), codex("cy", sessions=1)])
    monkeypatch.setattr(plugins, "claude_only", lambda profile: False)
    return tmp_path


@pytest.fixture
def claude_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(
        capacity, "_claude", lambda environ, now: [capacity.Account("claude", "cc", "OPEN", 0, 90, 90, 6)]
    )
    monkeypatch.setattr(capacity, "_codex", lambda environ, now, refresh: [])
    monkeypatch.setattr(plugins, "claude_only", lambda profile: False)
    return tmp_path


def _spawned(tmp_path, lanes, lane, quota_accounts=None):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run)
    if quota_accounts is not None:
        runtime._quota_accounts = quota_accounts
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes, autonomy="delegate"
    )
    placed = runtime.spawn(config, lane, f"{lane}@a1b2c3-0001", {"id": "t1", "title": "x"})
    return placed, seen["argv"]


@pytest.mark.parametrize("template", ["codex-only", "default"])
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("tick", [True, False])
def test_a_codex_only_env_spawns_the_master_and_every_lane_on_codex(codex_only, template, lane, tick):
    assert {row.harness for row in capacity.accounts({}, 0.0)} == {"codex"}
    lanes = templates.lane_map(templates.load(template, {}))
    rows = capacity.accounts({}, 0.0) if tick else None
    placed, argv = _spawned(codex_only, lanes, lane, rows)
    assert argv[argv.index("--agent") + 1] == "codex"
    assert placed.harness == "codex"


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("tick", [True, False])
def test_a_claude_only_template_spawns_every_lane_on_claude(claude_only, lane, tick):
    lanes = templates.lane_map(templates.load("claude-only", {}))
    placed, argv = _spawned(claude_only, lanes, lane, capacity.accounts({}, 0.0) if tick else None)
    assert argv[argv.index("--agent") + 1] == "claude"
    assert placed.harness == "claude"


def test_choose_names_the_harness_holding_an_account_when_every_seat_is_full(monkeypatch):
    monkeypatch.setattr(capacity, "accounts", lambda environ, now: [codex(cap=0, state="CLOSED")])
    assert agent_choice.choose("", {}) == ("codex", agent_choice.ALL_FULL)
    monkeypatch.setattr(capacity, "accounts", lambda environ, now: [])
    assert agent_choice.choose("", {}) == ("", agent_choice.ALL_FULL)


def test_fallback_names_a_harness_that_holds_an_account():
    both = [capacity.Account("claude", "a", "CLOSED", 0, 0, 0, 0), codex(cap=0, state="CLOSED")]
    assert agent_choice.fallback(both) == "claude"
    assert agent_choice.fallback([codex()]) == "codex"
    assert agent_choice.fallback([]) == ""


def test_preferring_puts_the_named_harness_first():
    assert agent_choice.preferring("codex") == ("codex", "claude")
    assert agent_choice.preferring("claude") == ("claude", "codex")
    assert agent_choice.preferring("") == ("claude", "codex")
    assert agent_choice.preferring("codex", ("claude",)) == ("claude",)


def test_rotation_with_every_codex_seat_full_names_codex(tmp_path):
    runtime = HerdrRuntime(home=tmp_path, choose=lambda requested, environ: pytest.fail("rotation asks no router"))
    runtime._quota_accounts = [codex(cap=0, state="CLOSED")]
    assert runtime._rotation("", {}) == ("codex", agent_choice.ALL_FULL)
    runtime._quota_accounts = []
    assert runtime._rotation("", {}) == ("", agent_choice.ALL_FULL)


def test_a_full_codex_only_swarm_refuses_the_spawn_without_guessing(codex_only):
    with pytest.raises(SpawnError) as error:
        _spawned(codex_only, {}, "eng", [codex(cap=0, state="CLOSED")])
    assert error.value.status == "unavailable"


def test_fill_takes_the_rotation_harness_when_nothing_is_saved(monkeypatch):
    monkeypatch.setattr(agent_choice, "choose", lambda requested, environ: ("codex", "rotation"))
    filled = master_launch.fill({}, SwarmConfig("sw", "/repo", 0, 0))
    assert filled == {"profile": "master", "harness": "codex", "model": "gpt-6.1-sol", "effort": "high"}


def test_fill_leaves_the_harness_empty_when_no_harness_has_an_account(monkeypatch):
    monkeypatch.setattr(agent_choice, "choose", lambda requested, environ: ("", agent_choice.ALL_FULL))
    assert master_launch.fill({"account": "a9"}, SwarmConfig("sw", "/repo", 0, 0)) == {
        "profile": "master",
        "account": "a9",
    }


def test_fill_leaves_the_harness_empty_when_every_seat_is_full(monkeypatch):
    monkeypatch.setattr(agent_choice, "choose", lambda requested, environ: ("codex", agent_choice.ALL_FULL))
    assert master_launch.fill({}, SwarmConfig("sw", "/repo", 0, 0)) == {"profile": "master"}


def test_fill_prefers_the_master_affinity_over_the_rotation(monkeypatch):
    monkeypatch.setattr(agent_choice, "choose", lambda requested, environ: pytest.fail("affinity decides"))
    config = SwarmConfig("sw", "/repo", 0, 0, lanes={MASTER: {"agent": "codex"}})
    assert master_launch.fill({}, config)["harness"] == "codex"


def test_harness_of_an_unknown_process_is_empty(monkeypatch):
    import hooks.proc

    monkeypatch.setattr(hooks.proc, "_process", lambda pid, proc: None)
    assert take_master.harness_of(1) == ""


def test_successor_follows_the_given_harness_order():
    both = [quota_handoff_account("a", "claude"), quota_handoff_account("cx", "codex")]
    thresholds = quota_handoff.Thresholds()
    assert quota_handoff.successor(both, ("codex", "claude"), thresholds).name == "cx"
    assert quota_handoff.successor(both, ("claude", "codex"), thresholds).name == "a"
    assert quota_handoff.successor(both, ("claude",), thresholds).name == "a"
    assert quota_handoff.successor([both[0]], ("codex",), thresholds) is None


def quota_handoff_account(name, harness):
    return capacity.Account(harness, name, "OPEN", 0, 90, 90, 3)


def test_a_codex_quota_handoff_stays_on_codex_when_both_harnesses_have_room(tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    saved = {"profile": "engineer", "harness": "codex", "model": "gpt-6.1-sol", "effort": "high", "account": "old"}
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: (requested, "requested"))
    runtime._quota_accounts = [
        capacity.Account("codex", "old", "OPEN", 0, 5, 90, 2),
        quota_handoff_account("a", "claude"),
        quota_handoff_account("fresh", "codex"),
    ]
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    task = {"id": "t1", "title": "x", "handoff": "h", "handoff_envelope": {"reason": "quota", "launch": saved}}
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)
    argv = seen["argv"]
    assert (argv[argv.index("--agent") + 1], argv[argv.index("--route") + 1]) == ("codex", "fresh")


def test_a_pinned_lane_agent_wins_over_a_claude_only_profile_in_quota_planning(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "claude_only", lambda profile: profile == "frontend")
    runtime = HerdrRuntime(home=tmp_path)
    config = SimpleNamespace(lanes={"eng": {"agent": "codex"}})
    ready = {"eng": [{"id": "t1", "profile": "frontend"}, {"id": "t2", "profile": "engineer"}]}
    assert runtime.quota_requirements(config, ready) == {"eng": [("codex",), ("codex",)]}
    config = SimpleNamespace(lanes={"eng": {"agent": "auto"}})
    assert runtime.quota_requirements(config, ready)["eng"][0] == ("claude",)


def test_a_claude_only_profile_in_a_codex_only_swarm_is_refused_in_plain_words(codex_only, monkeypatch):
    monkeypatch.setattr(plugins, "claude_only", lambda profile: profile == "frontend")
    lanes = templates.lane_map(templates.load("codex-only", {}))
    lanes["eng"] = {**lanes["eng"], "profile": "frontend"}
    with pytest.raises(SpawnError) as error:
        _spawned(codex_only, lanes, "eng", capacity.accounts({}, 0.0))
    assert str(error.value) == "lane harness codex cannot mount the claude only profile frontend"
    assert error.value.status == "unsupported"


@pytest.mark.parametrize(("name", "agent"), [("codex-only", "codex"), ("claude-only", "claude")])
def test_a_one_harness_template_pins_every_lane_and_round_trips(tmp_path, name, agent):
    environ = {"AGENTIHOOKS_HOME": str(tmp_path)}
    found = templates.load(name, environ)
    assert {lane: found.lanes[lane].agent for lane in LANES} == dict.fromkeys(LANES, agent)
    raw = json.loads((templates.BUILT_IN / f"{name}.json").read_text())
    assert templates.parse(raw) == found
    path = templates.save(found, environ)
    assert templates.load(name, environ) == found
    assert json.loads(path.read_text())["lanes"] == {key: asdict(value) for key, value in found.lanes.items()}
