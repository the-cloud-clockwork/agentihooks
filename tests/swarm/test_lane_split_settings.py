import pytest

from scripts.swarm import status
from tests.swarm import test_cli

env = test_cli.env
pytestmark = pytest.mark.unit


@pytest.fixture
def created(env, monkeypatch):
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    test_cli.run("sw", "create", "--repo", "/repo")
    return store


@pytest.mark.parametrize("pair", ["max-eng-agents=3", "max-ci-agents=8"])
def test_a_cap_the_operator_or_master_sets_is_pinned(created, pair):
    assert created.config("sw").caps_pinned is False
    assert test_cli.run("sw", "set", pair) == 0
    assert created.config("sw").caps_pinned is True


def test_other_settings_leave_the_caps_unpinned(created):
    assert test_cli.run("sw", "set", "compact-limit=500", "max-plan-agents=2") == 0
    assert created.config("sw").caps_pinned is False


@pytest.mark.parametrize("flag", ["--max-eng-agents", "--max-ci-agents"])
def test_caps_given_at_create_are_pinned(env, monkeypatch, flag):
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    test_cli.run("sw", "create", "--repo", "/repo", flag, "2")
    assert store.config("sw").caps_pinned is True


@pytest.mark.parametrize("mode, stored", [("deny", "enforce"), ("log only", "observe"), ("skip", "off")])
def test_the_operator_sets_the_lane_split_switch(created, mode, stored):
    assert (
        status.status_report(created, "sw", {"tasks": [], "_meta": {"events": []}})["gate_modes"]["lane-split"]
        == "log only"
    )
    assert test_cli.run("sw", "set", f"lane-split-gate={mode}") == 0
    assert created.config("sw").gates == {"lane-split": stored}
