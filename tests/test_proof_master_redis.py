"""A master pane launched for a scratch proof swarm keeps that swarm's Redis setting over the shared one."""

import subprocess
from pathlib import Path

import pytest

SCRATCH = "redis://127.0.0.1:6391/0"
SHARED = "redis://127.0.0.1:6379/0"
PROOF = "proof-323133-t1749-1"
THRESHOLD = "AGENTIHOOKS_HANDOFF_5H_PCT"


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


def _claude_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swarm: str) -> dict[str, str]:
    """The environment `agentihooks claude --route claude-ai` hands the session it starts, from a pane that exported
    the swarm and the scratch Redis while the env files set the shared Redis and a quota threshold."""
    from scripts import install
    from scripts.routing import claude_ai

    env_file = tmp_path / ".env"
    env_file.write_text(f"{THRESHOLD}=99\n")
    (tmp_path / "swarm-client.env").write_text(f"AGENTIHOOKS_SWARM_REDIS_URL={SHARED}\n")
    monkeypatch.setattr(install, "_ENV_FILE_DST", env_file)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", SCRATCH)
    monkeypatch.setenv(THRESHOLD, "5")
    started: dict[str, str] = {}

    def launch(binary: str, args: list[str], environ: dict[str, str], report: str) -> None:
        started.update(environ)

    monkeypatch.setattr(claude_ai, "launch_claude", launch)
    install.cmd_claude(["--route", "claude-ai", "--name", "master"])
    return started


@pytest.mark.feature("proof-master-claude-launch-scratch-redis")
def test_agentihooks_claude_starts_a_proof_master_on_the_scratch_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import store

    started = _claude_launch(tmp_path, monkeypatch, PROOF)
    assert started["AGENTIHOOKS_SWARM_REDIS_URL"] == SCRATCH
    assert store.redis_url(started) == SCRATCH
    assert started[THRESHOLD] == "99"


@pytest.mark.feature("proof-master-claude-launch-scratch-redis")
def test_agentihooks_claude_starts_a_live_swarm_session_on_the_env_file_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _claude_launch(tmp_path, monkeypatch, "rig-grade-swarm")
    assert started["AGENTIHOOKS_SWARM_REDIS_URL"] == SHARED
    assert started[THRESHOLD] == "99"


@pytest.mark.feature("proof-master-claude-launch-scratch-redis")
def test_only_a_proof_swarm_that_exported_its_redis_keeps_it_over_the_env_files() -> None:
    from scripts import runtime_env

    exported = {"AGENTIHOOKS_SWARM": PROOF, "AGENTIHOOKS_SWARM_REDIS_URL": SCRATCH, THRESHOLD: "5"}
    assert runtime_env.shell_kept(exported) == {"AGENTIHOOKS_SWARM_REDIS_URL"}
    assert runtime_env.shell_kept({"AGENTIHOOKS_SWARM": PROOF, THRESHOLD: "5"}) == set()
    assert runtime_env.shell_kept({"AGENTIHOOKS_SWARM_REDIS_URL": SCRATCH, THRESHOLD: "5"}) == set()
    assert runtime_env.shell_kept({**exported, "AGENTIHOOKS_SWARM": "rig-grade-swarm"}) == set()
