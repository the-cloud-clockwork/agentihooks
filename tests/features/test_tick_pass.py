import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts.swarm import cli, idle, launch_check
from scripts.swarm.ledger_client import LedgerClient
from tests.features.conftest import Agent, Swarm
from tests.features.test_harness import _until
from tests.swarm.test_tick import FakeRuntime


from scripts.swarm.ledger_client import _ledger

@pytest.mark.feature
def test_the_real_backend_feature_suite_holds_both_promotion_jobs() -> None:
    root = Path(__file__).resolve().parents[2]
    jobs = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]
    suite = jobs["feature-suite"]
    assert suite["uses"] == "./.github/workflows/feature-suite.yml"
    assert "github.event_name == 'push'" in suite["if"]
    assert "github.ref == 'refs/heads/dev'" in suite["if"]
    assert not suite.get("needs")
    for name in ("promotion-ready", "promote"):
        assert "feature-suite" in jobs[name]["needs"]
        assert "needs.feature-suite.result == 'success'" in jobs[name]["if"]


@pytest.mark.feature("swarm-tick-real-backends")
@pytest.mark.parametrize("agents", [3, 15])
def test_a_tick_pass_uses_real_backends_with_scripted_agents(
    agents: int,
    swarm: Swarm,
    scripted_agent_launch: Callable[..., Agent],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = [scripted_agent_launch() for _ in range(agents)]
    store, slug = swarm.store, swarm.ledger.slug
    for agent in launched:
        _until(lambda: idle.heartbeat(store.redis, slug, agent.record.name), agent, "a heartbeat")
        bound, misses = launch_check.canary(store, slug, agent.record)
        assert not misses
        assert bound.profile_decision["validation"]["pid"] == agent.process.pid
    for name, value in swarm.ledger.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    store.update(slug, state="paused", repo=str(tmp_path), snapshot_minutes=0)
    runtime = FakeRuntime(full=True)
    runtime.live.update(agent.record.name for agent in launched)
    ledger = LedgerClient()
    monkeypatch.setattr(_ledger(), "BASE", f"http://127.0.0.1:{swarm.ledger.env['LEDGER_PORT']}")
    before = int(time.time() * 1000)
    actions = cli.run_tick(store, slug, ledger=ledger, runtime=runtime)
    assert isinstance(actions, list)
    completed = store.redis.get(store.key(slug, "last-tick"))
    assert completed is not None, actions
    assert int(completed) >= before
    assert ledger.state(slug)["tasks"]
    assert all(agent.process.poll() is None for agent in launched)
    assert all(idle.heartbeat(store.redis, slug, agent.record.name) for agent in launched)
