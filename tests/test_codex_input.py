import sys

import pytest

from scripts import init_agent


@pytest.mark.parametrize("resume", ["", "conversation"])
def test_swarm_codex_launch_guards_the_routed_command(tmp_path, resume):
    spec = init_agent.AgentSpec(agent="codex", resume=resume)
    plain, before = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    guarded, guarded_before = init_agent._agent_command(
        spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path
    )
    assert guarded == [sys.executable, "-m", "scripts.codex_input", "--", *plain]
    assert guarded_before == before == ""


def test_swarm_claude_launch_keeps_its_command(tmp_path):
    spec = init_agent.AgentSpec()
    plain = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    assert init_agent._agent_command(
        spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path
    ) == plain
