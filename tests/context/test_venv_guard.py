"""Tests for hooks.context.venv_guard."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.config
from hooks import hook_manager
from hooks.context import quota_policy, venv_guard
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


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    venv = tmp_path / "workspace" / ".venv"
    (venv / "bin").mkdir(parents=True)
    primary = tmp_path / "workspace" / "repo"
    primary.mkdir()
    _git("-C", str(primary), "init", "-q")
    _git("-C", str(primary), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "worktrees" / "repo" / "wt"
    _git("-C", str(primary), "worktree", "add", "-q", str(worktree))
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    return {"venv": venv, "primary": primary, "worktree": worktree, "loose": loose}


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def no_policy(session: str) -> None:
        pass

    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path / "home")
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(quota_policy, "evaluate", no_policy)
    with patch("hooks._redis.get_redis", return_value=None):
        yield


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ('uv pip install --python {venv}/bin/python -e ".[all]"', "worktree"),
        ("uv pip install -p {venv} -e {worktree}", "primary"),
        ("uv pip install --python={venv}/bin/python --editable {worktree}", "primary"),
        ("{venv}/bin/pip install -e .", "worktree"),
        ("{venv}/bin/python -m pip install --editable={worktree}", "primary"),
        ("cd {worktree} && uv pip install --python {venv}/bin/python -e .", "primary"),
        ("V={venv}/bin; uv pip install --python $V/python -e .", "worktree"),
        ("uv pip install -e {worktree}", "primary"),
        ("uv pip install --python {venv}/bin/python -e {loose}", "primary"),
        ("uv pip install --python {venv}/bin/python -e git+https://github.com/o/r#egg=r", "primary"),
        ("uv pip install --python ~/workspace/.venv/bin/python -e .", "worktree"),
        ("VIRTUAL_ENV={venv} uv pip install -e .", "worktree"),
        ("export UV_PYTHON={venv}/bin/python && uv pip install -e .", "worktree"),
        ("VIRTUAL_ENV={worktree}/.venv uv pip install -e .", "worktree"),
        ("VIRTUAL_ENV=/elsewhere true && uv pip install -e .", "worktree"),
        ("git commit -m 'VIRTUAL_ENV=/elsewhere' && uv pip install -e .", "worktree"),
    ],
)
def test_editable_install_into_the_hooks_venv_from_outside_a_primary_checkout_is_refused(
    checkouts: dict[str, Path], command: str, cwd: str
) -> None:
    with pytest.raises(BlockAction, match="not inside a primary checkout"):
        check_venv_guard(_bash(command.format(**checkouts), checkouts[cwd]))


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
def test_editable_install_into_the_active_hooks_venv_from_a_worktree_is_refused(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", str(checkouts["venv"]))
    with pytest.raises(BlockAction, match="not inside a primary checkout"):
        check_venv_guard(_bash("pip install -e .", checkouts["worktree"]))


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
def test_the_refusal_names_the_hooks_venv_the_source_and_the_way_out(checkouts: dict[str, Path]) -> None:
    venv, worktree = checkouts["venv"], checkouts["worktree"]
    with pytest.raises(BlockAction) as refused:
        check_venv_guard(_bash(f"uv pip install --python {venv}/bin/python -e .", worktree))
    assert str(refused.value) == (
        f"BLOCKED: this editable install can point the venv the hooks run from ({venv}) at {worktree}, which is not "
        "inside a primary checkout. Once that folder goes, as wt.sh done removes a worktree, every hook and gate "
        "condition that imports from it fails open.\n"
        f"Install from the primary checkout (uv pip install --python {venv}/bin/python -e <primary checkout>), or "
        "name the worktree's own venv with --python."
    )


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
@pytest.mark.parametrize(
    ("command", "cwd"),
    [
        ('uv pip install --python {venv}/bin/python -e ".[all]"', "primary"),
        ("uv pip install --python {venv}/bin/python -e {primary}", "worktree"),
        ("uv pip install -e .", "primary"),
        ("cd {worktree}/missing && uv pip install --python {venv}/bin/python -e .", "primary"),
        ("VIRTUAL_ENV={worktree}/.venv uv pip install --python {worktree}/.venv/bin/python -e .", "worktree"),
        ("uv pip install --python {worktree}/.venv/bin/python -e .", "worktree"),
        ("uv pip install -e .", "worktree"),
        ("pip install -e .", "worktree"),
        ("uv pip install --python {venv}/bin/python requests", "worktree"),
        ("uv pip install --python {venv}/bin/python -e", "worktree"),
        ("{venv}/bin/pip download -e {worktree}", "primary"),
        ("{venv}/bin/ruff check -e {worktree}", "primary"),
        ("git commit -m 'uv pip install --python {venv}/bin/python -e .'", "worktree"),
    ],
)
def test_editable_install_from_a_primary_checkout_or_into_another_venv_passes(
    checkouts: dict[str, Path], command: str, cwd: str
) -> None:
    check_venv_guard(_bash(command.format(**checkouts), checkouts[cwd]))


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
def test_an_install_from_a_primary_checkout_whose_path_holds_a_space_passes(checkouts: dict[str, Path]) -> None:
    primary = checkouts["venv"].parent / "work space"
    primary.mkdir()
    _git("-C", str(primary), "init", "-q")
    check_venv_guard(_bash(f"uv pip install --python {checkouts['venv']}/bin/python -e '{primary}'", primary))


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
def test_the_shell_guards_refuse_a_worktree_editable_install_into_the_hooks_venv(
    checkouts: dict[str, Path], isolated: None
) -> None:
    payload = _bash(f"uv pip install --python {checkouts['venv']}/bin/python -e .", checkouts["worktree"])
    with pytest.raises(BlockAction, match="not inside a primary checkout"):
        hook_manager._check_shell_guards({**payload, "session_id": "s1"})


@pytest.mark.feature("venv-guard-refuses-worktree-editable-install")
def test_the_shell_guards_warn_when_the_venv_guard_crashes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], isolated: None
) -> None:
    logged: list[tuple[object, ...]] = []

    def crash(payload: dict[str, object]) -> None:
        raise RuntimeError("boom")

    def record(*args: object) -> None:
        logged.append(args)

    monkeypatch.setattr(venv_guard, "check_venv_guard", crash)
    monkeypatch.setattr(hook_manager, "log", record)
    hook_manager._check_shell_guards({"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "echo hi"}})
    assert ("venv_guard check failed", {"error": "boom"}) in logged
    assert "WARNING: venv_guard check failed (boom) — guard bypassed\n" in capsys.readouterr().err
