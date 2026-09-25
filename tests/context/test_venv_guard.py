"""Tests for hooks.context.venv_guard."""

import pytest

from hooks.context.venv_guard import check_venv_guard
from hooks.hook_manager import BlockAction


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    shared = tmp_path / ".venv"
    project = tmp_path / "repo"
    (project / "sub").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(shared))
    return tmp_path, shared, project


def _bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


@pytest.mark.parametrize(
    "command",
    [
        "uv run --active python -m pytest",
        "uv sync --active",
        "uv run --active ruff check .",
    ],
)
def test_active_sync_of_shared_venv_blocked(workspace, command):
    _, _, project = workspace
    with pytest.raises(BlockAction, match="not this project's own .venv"):
        check_venv_guard(_bash(command, project / "sub"))


def test_cd_prefix_resolves_project(workspace):
    _, _, project = workspace
    with pytest.raises(BlockAction):
        check_venv_guard(_bash(f"cd {project} && uv run --active pytest", "/"))


def test_project_own_venv_allowed(workspace, monkeypatch):
    _, _, project = workspace
    monkeypatch.setenv("VIRTUAL_ENV", str(project / ".venv"))
    check_venv_guard(_bash("uv sync --active", project))


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m pytest",
        "uv pip install --python /x/.venv/bin/python requests",
        "echo 'never uv run --active'",
    ],
)
def test_other_commands_allowed(workspace, command):
    _, _, project = workspace
    check_venv_guard(_bash(command, project))


def test_no_active_venv_allowed(workspace, monkeypatch):
    _, _, project = workspace
    monkeypatch.delenv("VIRTUAL_ENV")
    check_venv_guard(_bash("uv sync --active", project))
