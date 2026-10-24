import pytest

from scripts.gates import catalog
from scripts.swarm import status
from tests.swarm import test_cli

env = test_cli.env
pytestmark = pytest.mark.unit


def test_watchdog_mode_defaults_to_log_only_in_status(env, monkeypatch):
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    test_cli.run("sw", "create", "--repo", "/repo")
    assert catalog.defaults()["stall"] == "observe"
    report = status.status_report(store, "sw", {"tasks": [], "_meta": {"events": []}})
    assert report["gate_modes"]["stall"] == "log only"


@pytest.mark.parametrize(
    "mode, stored",
    [
        ("deny", "enforce"),
        ("log only", "observe"),
        ("skip", "off"),
        ("enforce", "enforce"),
        ("observe", "observe"),
        ("off", "off"),
    ],
)
def test_operator_sets_the_watchdog_mode_with_existing_gate_names(env, monkeypatch, mode, stored):
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    test_cli.run("sw", "create", "--repo", "/repo")
    assert test_cli.run("sw", "set", f"stall-gate={mode}") == 0
    assert store.config("sw").gates == {"stall": stored}
