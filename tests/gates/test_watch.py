"""The watch budget: a swarm agent that has only watched since its last action is refused another watch call."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.gates import entry
from scripts.gates.base import Call, Gate, Who
from scripts.gates.watch import WatchBudget, watcher_alive
from scripts.swarm.health import activity, findings

ENG, CI, MASTER, SLUG = "sw-eng-1", "sw-ci-1", "sw-master-1", "sw"
CHECKS = {"command": "gh pr checks 12"}
REARM = {"command": f"agentihooks ledger watch {SLUG} --as {ENG}"}


@pytest.fixture(autouse=True)
def no_redis():
    with patch("hooks._redis.get_redis", return_value=None):
        yield


@pytest.fixture
def gate(tmp_path):
    return WatchBudget(root=tmp_path / "activity", environ={"LEDGER_DIR": str(tmp_path / "ledgers")})


def who(name=ENG, swarm=SLUG):
    return Who(name=name, swarm=swarm)


def made(gate, name=ENG, watch=0, act=0, rearm=False):
    tool_input = REARM if rearm else CHECKS
    for _ in range(act):
        activity.record(
            "Bash", {"command": "git push"}, {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": name}, gate.root
        )
    for _ in range(watch):
        activity.record("Bash", tool_input, {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": name}, gate.root)


def decide(gate, name=ENG, tool_input=CHECKS, tool="Bash", swarm=SLUG):
    return gate.decide(Call(tool=tool, tool_input=tool_input), who(name, swarm), None)


def beat(gate, name=ENG, age=0.0):
    path = gate.ledger_dir / ".sessions" / f"{SLUG}.{name}.watch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    at = time.time() - age
    os.utime(path, (at, at))
    return path


class TestMatches:
    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("Monitor", REARM),
            ("TaskOutput", {}),
            ("BashOutput", {}),
            ("Bash", CHECKS),
            ("Bash", {"command": "gh run watch 9"}),
            ("Bash", {"command": "sleep 30"}),
            ("Bash", REARM),
        ],
    )
    def test_matches_every_call_classify_labels_watch(self, gate, tool, tool_input):
        assert gate.matches(Call(tool=tool, tool_input=tool_input))

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("Edit", {}),
            ("Read", {}),
            ("Bash", {"command": "git push"}),
            ("Bash", {"command": "agentihooks swarm sw status"}),
            ("Bash", {"command": "ls"}),
        ],
    )
    def test_skips_every_call_classify_does_not_label_watch(self, gate, tool, tool_input):
        assert not gate.matches(Call(tool=tool, tool_input=tool_input))

    def test_is_a_gate_registered_under_its_name(self, gate):
        assert isinstance(gate, Gate)
        assert (gate.name, gate.default_mode) == ("watch", "enforce")
        assert isinstance(entry.GATES["watch"], WatchBudget)


class TestDecide:
    def test_the_twenty_first_watch_with_no_action_is_refused(self, gate):
        made(gate, watch=20)
        decision = decide(gate)
        assert not decision.allowed
        assert decision.reason == (
            "watch budget: 20 watch calls since your last action (limit 20) and 20 watch calls for 0 actions "
            "(limit 5 per action). Hand the waiting to the tick: agentihooks swarm sw wait <minutes> --reason "
            '"<what you wait on>", or act first: commit, push or record progress on the ledger.'
        )

    def test_the_twentieth_watch_passes(self, gate):
        made(gate, watch=19)
        assert decide(gate).allowed

    def test_an_action_resets_the_budget(self, gate):
        made(gate, watch=20)
        made(gate, act=1)
        assert decide(gate).allowed

    def test_watches_after_the_action_count_again(self, gate):
        made(gate, watch=20, act=1)
        made(gate, watch=20)
        assert not decide(gate).allowed

    def test_a_busy_agent_keeps_watching_while_its_lifetime_ratio_holds(self, gate):
        made(gate, act=6)
        made(gate, watch=29)
        assert decide(gate).allowed
        made(gate, watch=1)
        assert not decide(gate).allowed

    def test_a_ci_agent_has_the_same_budget(self, gate):
        made(gate, name=CI, watch=20)
        assert not decide(gate, name=CI).allowed

    def test_the_master_has_the_looser_budget(self, gate):
        made(gate, name=MASTER, watch=59)
        assert decide(gate, name=MASTER).allowed
        made(gate, name=MASTER, watch=1)
        decision = decide(gate, name=MASTER)
        assert not decision.allowed
        assert "(limit 60)" in decision.reason and "(limit 15 per action)" in decision.reason

    def test_limits_come_from_the_health_environment(self, tmp_path):
        env = {"LEDGER_DIR": str(tmp_path), "AGENTIHOOKS_HEALTH_WATCH_MIN": "3", "AGENTIHOOKS_HEALTH_WATCH_RATIO": "2"}
        gate = WatchBudget(root=tmp_path / "activity", environ=env)
        made(gate, watch=2)
        assert decide(gate).allowed
        made(gate, watch=1)
        assert "(limit 3)" in decide(gate).reason

    def test_outside_a_swarm_everything_passes(self, gate):
        made(gate, watch=40)
        assert decide(gate, swarm="").allowed

    @pytest.mark.parametrize("name", ["planner@323133-0001", "planner-1", "operator"])
    def test_names_outside_the_gated_lanes_pass(self, gate, name):
        made(gate, name=name, watch=40)
        assert decide(gate, name=name).allowed

    def test_an_operator_launched_engineer_passes(self, gate):
        made(gate, watch=40)
        operator = Who(name=ENG, swarm=SLUG, lane="operator")
        assert gate.decide(Call(tool="Bash", tool_input=CHECKS), operator, None).allowed
        assert not gate.decide(
            Call(tool="Bash", tool_input=CHECKS), Who(name=ENG, swarm=SLUG, lane="eng"), None
        ).allowed

    def test_an_unsafe_name_passes(self, gate):
        assert decide(gate, name="../sw-eng-1").allowed

    def test_an_unsafe_swarm_passes_even_where_its_path_resolves_to_real_rows(self, gate):
        made(gate, watch=40)
        (gate.root / "x").mkdir()
        assert decide(gate, swarm="x/../sw").allowed

    def test_a_rearm_with_no_watcher_passes(self, gate):
        made(gate, watch=20)
        assert decide(gate, tool="Monitor", tool_input=REARM).allowed

    def test_a_rearm_with_a_stale_watcher_passes(self, gate):
        made(gate, watch=20)
        beat(gate, age=60)
        assert decide(gate, tool="Monitor", tool_input=REARM).allowed

    def test_a_rearm_with_a_live_watcher_is_refused(self, gate):
        made(gate, watch=20)
        beat(gate)
        assert not decide(gate, tool="Monitor", tool_input=REARM).allowed

    def test_a_live_watcher_does_not_free_other_watch_calls(self, gate):
        made(gate, watch=20)
        assert not decide(gate).allowed

    def test_a_rearm_let_through_for_a_dead_watcher_leaves_the_budget_and_health_unchanged(self, gate):
        made(gate, watch=20)
        assert decide(gate, tool="Monitor", tool_input=REARM).allowed
        made(gate, watch=1, rearm=True)
        counts = activity.counts(SLUG, gate.root)[ENG]
        assert counts == {"watch": 20, "act": 0, "since": 20}
        assert not findings.over_watched(counts, *findings.watch_limits(ENG, findings.limits({})))
        assert not decide(gate).allowed

    def test_a_rearm_with_a_live_watcher_still_counts(self, gate):
        made(gate, watch=19)
        beat(gate)
        assert decide(gate, tool="Monitor", tool_input=REARM).allowed
        made(gate, watch=1, rearm=True)
        assert activity.counts(SLUG, gate.root)[ENG] == {"watch": 20, "act": 0, "since": 20}


class TestWatcherAlive:
    def test_a_fresh_heartbeat_is_alive(self, gate):
        beat(gate)
        assert watcher_alive(who(), gate.ledger_dir)

    def test_a_heartbeat_at_the_stale_limit_is_alive(self, gate):
        path = beat(gate)
        assert watcher_alive(who(), gate.ledger_dir, now=path.stat().st_mtime + 20)
        assert not watcher_alive(who(), gate.ledger_dir, now=path.stat().st_mtime + 21)

    def test_no_heartbeat_is_dead(self, gate):
        assert not watcher_alive(who(), gate.ledger_dir)

    def test_another_agents_heartbeat_does_not_count(self, gate):
        beat(gate, name=CI)
        assert not watcher_alive(who(), gate.ledger_dir)

    def test_an_alias_heartbeat_counts(self, gate):
        beat(gate, name="engineer@1-1")
        with patch("scripts.swarm.naming.addresses", return_value=[ENG, "engineer@1-1"]):
            assert watcher_alive(who(), gate.ledger_dir)


def test_the_ledger_dir_defaults_to_the_development_ledger(tmp_path):
    assert WatchBudget(environ={}).ledger_dir == Path.home() / "development-ledger"
    assert WatchBudget(environ={"LEDGER_DIR": str(tmp_path)}).ledger_dir == tmp_path
    assert WatchBudget(environ={"LEDGER_DIR": "~/x"}).ledger_dir == Path("~/x").expanduser()


def test_the_activity_root_defaults_to_the_recorder_root():
    assert WatchBudget(environ={}).root == activity.default_root()
