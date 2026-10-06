"""The model and effort a session opened by hand runs on: its launch flags, else its harness config."""

import json
import tomllib

from hooks.targets import codex_home
from scripts.claude_config import claude_home

CODEX_CONFIG = ("-c", "--config")


def _flag(argv, names):
    for i, arg in enumerate(argv):
        if arg in names and i + 1 < len(argv):
            return argv[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return ""


def _codex_overrides(argv):
    values = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in CODEX_CONFIG]
    values += [arg.split("=", 1)[1] for arg in argv if arg.startswith("--config=")]
    pairs = (value.partition("=") for value in values)
    return {key.strip(): raw.strip().strip("\"'") for key, _, raw in pairs}


def _codex_config():
    try:
        config = tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    return str(config.get("model") or ""), str(config.get("model_reasoning_effort") or "")


def _claude_config():
    try:
        settings = json.loads((claude_home() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(settings, dict):
        return "", ""
    return str(settings.get("model") or ""), str(settings.get("effortLevel") or "")


def read(harness, argv):
    if harness == "codex":
        overrides = _codex_overrides(argv)
        model = _flag(argv, ("-m", "--model")) or overrides.get("model", "")
        effort = overrides.get("model_reasoning_effort", "")
        fallback = _codex_config
    else:
        model, effort = _flag(argv, ("--model",)), _flag(argv, ("--effort",))
        fallback = _claude_config
    if model and effort:
        return model, effort
    config_model, config_effort = fallback()
    return model or config_model, effort or config_effort
