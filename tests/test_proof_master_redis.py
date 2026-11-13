"""A master pane launched for a scratch proof swarm keeps that swarm's Redis setting over the shared one."""

import subprocess
from pathlib import Path

import pytest

SCRATCH = "redis://127.0.0.1:6391/0"
SHARED = "redis://127.0.0.1:6379/0"
PROOF = "proof-323133-t1749-1"


def _pane_redis(environ: dict[str, str]) -> str:
    """The Redis setting a pane holds after the operator env set the shared one and the launcher's swarm exports ran."""
    from scripts import init_agent

    script = f"export AGENTIHOOKS_SWARM_REDIS_URL={SHARED}\n{init_agent._swarm_exports(environ)}"
    script += 'printf %s "$AGENTIHOOKS_SWARM_REDIS_URL"'
    return subprocess.run(["bash", "-c", script], env={}, capture_output=True, text=True, check=True).stdout


@pytest.mark.feature("proof-master-launch-scratch-redis")
def test_a_proof_swarm_master_pane_reads_the_scratch_redis(tmp_path: Path) -> None:
    from scripts import init_agent

    environ = {
        "XDG_RUNTIME_DIR": str(tmp_path),
        "AGENTIHOOKS_SWARM": PROOF,
        "AGENTIHOOKS_SWARM_LANE": "master",
        "AGENTIHOOKS_SWARM_REDIS_URL": SCRATCH,
    }
    launcher, _ = init_agent._write_launcher(tmp_path, "master", "", [], environ, init_agent.AgentSpec(agent="claude"))
    text = launcher.read_text()
    assert f"export AGENTIHOOKS_SWARM_REDIS_URL={SCRATCH}\n" in text
    assert text.index("AGENTIHOOKS_SWARM_REDIS_URL") < text.index(" claude ")
    assert _pane_redis(environ) == SCRATCH


@pytest.mark.feature("proof-master-launch-scratch-redis")
def test_a_live_swarm_pane_keeps_the_operator_redis(tmp_path: Path) -> None:
    environ = {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_SWARM_REDIS_URL": SCRATCH}
    assert _pane_redis(environ) == SHARED
