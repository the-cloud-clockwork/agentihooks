import re
import shlex
import sys
from pathlib import Path

import pytest

from scripts import init_agent

pytestmark = pytest.mark.unit

HIVE = {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SLOT_HIVE": "hive-a", "HIVE_GITHUB_AUTH_MODE": "app"}
APP_SERVER = init_agent.AgentSpec(
    agent="codex", profile="engineer", transport=init_agent.APP_SERVER, seat="eng-1@rig", execution="exe-1"
)
CASES = {
    "claude": ({}, init_agent.AgentSpec(), {"access_env", "session_exit"}),
    "claude on a hive": (HIVE, init_agent.AgentSpec(), {"access_env", "session_exit", "github_app_cli"}),
    "codex in a swarm": (
        {"AGENTIHOOKS_SWARM": "rig"},
        init_agent.AgentSpec(agent="codex"),
        {"access_env", "session_exit", "codex_input"},
    ),
    "codex app server on a hive": (
        HIVE,
        APP_SERVER,
        {"access_env", "session_exit", "github_app_cli", "codex_session", "hive.launch_logs"},
    ),
}


def _nested(token):
    try:
        return _words(token)
    except ValueError:
        return [token]


def _words(text):
    tokens = shlex.split(text)
    return [word for token in tokens for word in (_nested(token) if re.search(r"\s", token) else [token])]


def _is_python(word):
    path = word.removeprefix("$(")
    return path == sys.executable or re.fullmatch(r"python[\d.]*", Path(path).name) is not None


@pytest.mark.parametrize("case", CASES)
@pytest.mark.feature("init-agent-launcher-python-safe-path")
def test_every_python_line_in_the_launcher_runs_with_the_safe_path_flag(tmp_path, case):
    environ, spec, expected = CASES[case]
    launcher, _ = init_agent._write_launcher(
        tmp_path, "eng", "Open the task.", [], {"XDG_RUNTIME_DIR": str(tmp_path), **environ}, spec
    )
    unsafe, modules = [], set()
    for line in launcher.read_text().splitlines():
        words = _words(line)
        for index, word in enumerate(words):
            if _is_python(word) and words[index + 1 : index + 2] != ["-P"]:
                unsafe.append(line)
            if word == "-m" and words[index + 1 : index + 2] and words[index + 1].startswith("scripts."):
                modules.add(words[index + 1].removeprefix("scripts."))
                if words[index - 1] != "-P":
                    unsafe.append(line)
    assert unsafe == []
    assert modules == expected
