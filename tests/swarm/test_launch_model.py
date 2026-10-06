import json
from pathlib import Path

import pytest

from scripts.swarm import launch_model


@pytest.fixture
def homes(tmp_path, monkeypatch):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}


@pytest.mark.parametrize(
    "harness,argv,expected",
    [
        ("claude", ("claude", "--model", "opus", "--effort", "high"), ("opus", "high")),
        ("claude", ("claude", "--model=sonnet", "--effort=max"), ("sonnet", "max")),
        ("codex", ("codex", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"'), ("gpt-6.1-sol", "high")),
        ("codex", ("codex", "--model=gpt-6", "--config", "model_reasoning_effort=low"), ("gpt-6", "low")),
        ("codex", ("codex", "-c", 'model="gpt-6"', "-c", "model_reasoning_effort='xhigh'"), ("gpt-6", "xhigh")),
    ],
)
def test_launch_flags_name_the_model_and_effort(homes, harness, argv, expected):
    assert launch_model.read(harness, argv) == expected


def test_codex_falls_back_to_its_config(homes):
    (Path(homes["CODEX_HOME"]) / "config.toml").write_text(
        'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "medium"\n\n[profiles.x]\nmodel = "other"\n'
    )
    assert launch_model.read("codex", ("codex",)) == ("gpt-6.1-sol", "medium")
    assert launch_model.read("codex", ("codex", "-c", "model_reasoning_effort=high")) == ("gpt-6.1-sol", "high")


def test_claude_falls_back_to_its_settings(homes):
    (Path(homes["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(
        json.dumps({"model": "opus", "effortLevel": "high"})
    )
    assert launch_model.read("claude", ("claude",)) == ("opus", "high")
    assert launch_model.read("claude", ("claude", "--model", "sonnet")) == ("sonnet", "high")


def test_nothing_named_and_unreadable_config_leave_both_empty(homes):
    assert launch_model.read("codex", ("codex",)) == ("", "")
    (Path(homes["CODEX_HOME"]) / "config.toml").write_text("model = [")
    (Path(homes["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text("{")
    assert launch_model.read("codex", ("codex",)) == ("", "")
    assert launch_model.read("claude", ("claude",)) == ("", "")
