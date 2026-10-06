"""The model and effort a session opened by hand runs on: its launch flags, else its harness config."""

import json
import tomllib

from hooks.targets import codex_home
from scripts.claude_config import claude_home

CODEX_EFFORT = "model_reasoning_effort"


def _values(argv, names):
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in names]


def _flag(argv, names):
    return next(iter(_values(argv, names)), "")


def _codex_effort(argv):
    pairs = (value.partition("=") for value in _values(argv, ("-c", "--config")))
    return next((raw.strip("\"'") for key, _, raw in pairs if key == CODEX_EFFORT), "")


def _codex_config():
    try:
        config = tomllib.loads((codex_home() / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    return config.get("model", ""), config.get(CODEX_EFFORT, "")


def _claude_config():
    try:
        settings = json.loads((claude_home() / "settings.json").read_text())
    except (OSError, ValueError):
        return "", ""
    return settings.get("model", ""), settings.get("effortLevel", "")


def read(harness, argv):
    if harness == "codex":
        model, effort = _flag(argv, ("-m", "--model")), _codex_effort(argv)
        config_model, config_effort = _codex_config()
    else:
        model, effort = _flag(argv, ("--model",)), _flag(argv, ("--effort",))
        config_model, config_effort = _claude_config()
    return model or config_model, effort or config_effort
