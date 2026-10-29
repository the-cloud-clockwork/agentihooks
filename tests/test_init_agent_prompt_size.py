import json
import subprocess
import sys

import pytest

from scripts import init_agent
from scripts.profiles import binding


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("prompt", ["x" * 200_000, "é" * 65_536], ids=["ascii", "multibyte"])
def test_large_opening_prompt_launches_without_exceeding_the_argument_limit(tmp_path, monkeypatch, agent, prompt):
    command = [
        sys.executable,
        "-c",
        "import json,sys; print(json.dumps(sys.argv[1:]))",
    ]
    before = ": capture-opening\n"
    monkeypatch.setattr(init_agent, "_agent_command", lambda *args: (command, before))
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda *args: {})
    launcher, opening = init_agent._write_launcher(
        tmp_path, "master", prompt, [], {}, init_agent.AgentSpec(agent=agent)
    )
    line = launcher.read_text().split(before, 1)[1].split("\nrm -f ", 1)[0]

    result = subprocess.run(["bash", "-c", line], capture_output=True, text=True, check=False, timeout=10)

    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)
    assert all(len(argument.encode()) < 131_072 for argument in arguments)
    assert str(opening) in arguments[-1]
    assert binding.PROMPT in arguments[-1]
    assert opening.read_text() == prompt


def test_large_codex_opening_delivered_after_start_keeps_its_file_and_binding_instruction(tmp_path, monkeypatch):
    prompt = "é" * 65_536
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda *args: {})
    launcher, opening = init_agent._write_launcher(
        tmp_path, "master", prompt, [], {}, init_agent.AgentSpec(agent="codex")
    )

    delivered = init_agent._held_prompt(launcher, "codex")

    assert len(delivered.encode()) < 131_072
    assert str(opening) in delivered
    assert binding.PROMPT in delivered
    assert opening.read_text() == prompt
