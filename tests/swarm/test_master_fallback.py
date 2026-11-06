from dataclasses import dataclass, field

import pytest

from scripts.swarm import master_fallback

pytestmark = pytest.mark.unit
MINUTE = 60_000


@dataclass
class Agent:
    name: str
    lane: str = "master"


@dataclass
class Config:
    state: str = "running"


@dataclass
class Store:
    agents_by_slug: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)

    def agents(self, slug):
        return self.agents_by_slug.get(slug, [])

    def config(self, slug):
        return Config(self.states.get(slug, "running"))


def _launches():
    started = []

    def launch(slug):
        started.append(slug)
        return 0

    return started, launch


def test_the_fallback_minutes_default_to_twenty_and_follow_the_env():
    assert master_fallback.minutes({}) == 20
    assert master_fallback.minutes({master_fallback.MINUTES: "7"}) == 7


def test_the_watched_swarms_come_from_the_env():
    assert master_fallback.swarms({master_fallback.SWARMS: " a, b ,,"}) == ["a", "b"]
    assert master_fallback.swarms({}) == []


def test_no_master_for_the_window_starts_exactly_one_master():
    store, down = Store({"sw": [Agent("master@x-1")]}), {}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"}

    assert master_fallback.check(store, env, set, 0, down, launch) == []
    assert master_fallback.check(store, env, set, 20 * MINUTE - 1, down, launch) == []
    lines = master_fallback.check(store, env, set, 20 * MINUTE, down, launch)

    assert started == ["sw"]
    assert '"master_up": 0' in lines[0]
    assert master_fallback.check(store, env, set, 21 * MINUTE, down, launch) == []
    assert started == ["sw"]


def test_a_live_master_resets_the_wait():
    store, down = Store({"sw": [Agent("master@x-1"), Agent("engineer@x-2", "eng")]}), {}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw"}

    master_fallback.check(store, env, set, 0, down, launch)
    master_fallback.check(store, env, lambda: {"master@x-1"}, 15 * MINUTE, down, launch)
    master_fallback.check(store, env, set, 30 * MINUTE, down, launch)

    assert started == []
    assert down == {"sw": 30 * MINUTE}


def test_a_live_engineer_is_no_master():
    store = Store({"sw": [Agent("engineer@x-2", "eng")]})

    assert master_fallback.master_live(store, "sw", {"engineer@x-2"}) is False


def test_a_stopped_swarm_is_left_alone():
    store, down = Store({"sw": []}, {"sw": "stopped"}), {"sw": 0}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw"}

    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == []
    assert started == []
    assert down == {}


def test_the_timer_runs_one_pass_a_minute_with_the_watched_swarms_and_minutes():
    units = master_fallback.unit_text("/py", "/repo", {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"})
    service = units["agentihooks-master-fallback.service"]
    timer = units["agentihooks-master-fallback.timer"]

    assert 'ExecStart="/py" -m scripts.swarm.master_fallback run' in service
    assert "Type=oneshot" in service
    assert f"Environment={master_fallback.SWARMS}=sw" in service
    assert f"Environment={master_fallback.MINUTES}=20" in service
    assert "WorkingDirectory=/repo" in service
    assert "OnUnitActiveSec=60" in timer
    assert "OnActiveSec=60" in timer
    assert "WantedBy=timers.target" in timer


def test_the_outage_starts_survive_between_passes(tmp_path):
    state = tmp_path / "fallback.json"

    assert master_fallback.load(state) == {}
    state.write_text('{"sw": 5}')
    assert master_fallback.load(state) == {"sw": 5}
