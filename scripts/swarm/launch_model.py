"""The model and effort a session opened by hand runs on: its launch flags, else its harness config."""

import json
import tomllib
from pathlib import Path

from hooks.targets import codex_home
from scripts.claude_config import claude_home

CLAUDE_FLAGS = {"--model": "model", "--effort": "effort"}
CLAUDE_KEYS = {"model": "model", "effortLevel": "effort"}
CODEX_FLAGS = {"-m": "model", "--model": "model"}
CODEX_KEYS = {"model": "model", "model_reasoning_effort": "effort"}
CODEX_OVERRIDE = ("-c", "--config")


def _named(argv, flags):
    return {flags[arg]: argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in flags}


def _renamed(values, keys):
    return {name: values[key] for key, name in keys.items() if key in values}


def _toml_value(raw):
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _codex_overrides(argv):
    pairs = (argv[i + 1].partition("=") for i, arg in enumerate(argv[:-1]) if arg in CODEX_OVERRIDE)
    return {key: _toml_value(raw) for key, _, raw in pairs}


def _load(path, parse, error):
    try:
        return parse(path.read_text())
    except (OSError, error):
        return {}


def configured(harness: str, home: Path | None = None) -> dict:
    if harness == "codex":
        return _renamed(
            _load((home or codex_home()) / "config.toml", tomllib.loads, tomllib.TOMLDecodeError), CODEX_KEYS
        )
    return _renamed(_load((home or claude_home()) / "settings.json", json.loads, ValueError), CLAUDE_KEYS)


def read(harness: str, argv: tuple[str, ...]) -> tuple[str, str]:
    if harness == "codex":
        flags = {**_renamed(_codex_overrides(argv), CODEX_KEYS), **_named(argv, CODEX_FLAGS)}
    else:
        flags = _named(argv, CLAUDE_FLAGS)
    found = {"model": "", "effort": "", **configured(harness), **flags}
    return found["model"], found["effort"]
