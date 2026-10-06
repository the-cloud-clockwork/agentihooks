import os
from collections.abc import Mapping
from pathlib import Path


def claude_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"]).expanduser()
    if env.get("CLAUDE_CODE_HOME_DIR"):
        return Path(env["CLAUDE_CODE_HOME_DIR"]).expanduser() / ".claude"
    if env.get("AGENTIHOOKS_CLAUDE_HOME"):
        return Path(env["AGENTIHOOKS_CLAUDE_HOME"]).expanduser()
    return Path.home() / ".claude"


def claude_json(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("CLAUDE_CONFIG_DIR"):
        return claude_home(env) / ".claude.json"
    home = env.get("HOME") if environ is not None else None
    return Path(env.get("CLAUDE_CODE_HOME_DIR") or home or Path.home()).expanduser() / ".claude.json"
