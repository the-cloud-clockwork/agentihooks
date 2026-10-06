import subprocess
from pathlib import Path

import pytest

from scripts import install


@pytest.fixture
def wheel_install(tmp_path, monkeypatch):
    scripts = tmp_path / "env" / "bin"
    scripts.mkdir(parents=True)
    (scripts / "agentihooks").write_text("#!/bin/sh\n")
    printed = []
    monkeypatch.setattr(install, "_cprint", printed.append)
    monkeypatch.setattr(install, "_is_source_checkout", lambda: False)
    monkeypatch.setattr(install.sysconfig, "get_path", lambda name: str(scripts) if name == "scripts" else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"unexpected subprocess {a}"))
    return scripts / "agentihooks", Path.home() / ".local" / "bin" / "agentihooks", printed


def test_wheel_install_links_its_own_cli_without_the_package_index(wheel_install):
    target, link, printed = wheel_install

    install._install_cli_tool()

    assert link.is_symlink()
    assert link.readlink() == target
    assert printed == [f"  [OK] CLI linked: {link} -> {target}"]


def test_a_stale_cli_link_is_replaced(wheel_install, tmp_path):
    target, link, printed = wheel_install
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old" / "agentihooks")

    install._install_cli_tool()

    assert link.readlink() == target


def test_a_cli_link_already_on_this_environment_is_kept(wheel_install):
    target, link, printed = wheel_install
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    before = link.lstat().st_ino

    install._install_cli_tool()

    assert link.lstat().st_ino == before
    assert printed == [f"  [OK] CLI on PATH: {link} is {target}"]


def test_a_missing_console_script_leaves_no_link(wheel_install):
    target, link, printed = wheel_install
    target.unlink()

    install._install_cli_tool()

    assert not link.is_symlink()
    assert printed == [f"  [!!] {target} not found — the agentihooks CLI was not linked"]


def test_uninstall_removes_the_cli_link(wheel_install, monkeypatch):
    target, link, printed = wheel_install
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    assert install._cli_tool_is_installed() is True
    install._uninstall_cli_tool()

    assert not link.is_symlink()
    assert target.exists()
    assert printed == [f"  [OK] Removed CLI link: {link}"]


def _uv_run(calls, returncode=0, stderr=""):
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run


def test_a_source_checkout_installs_the_editable_tree(wheel_install, monkeypatch):
    printed = wheel_install[2]
    calls = []
    monkeypatch.setattr(install, "_is_source_checkout", lambda: True)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(subprocess, "run", _uv_run(calls))

    install._install_cli_tool()

    assert calls == [
        (
            ["/bin/uv", "tool", "install", "--editable", "--force", "."],
            {"cwd": str(install.AGENTIHOOKS_ROOT), "capture_output": True, "text": True},
        )
    ]
    assert printed == ["  [OK] CLI installed via: uv tool install --editable ."]


def test_a_failed_editable_install_reports_uv(wheel_install, monkeypatch):
    printed = wheel_install[2]
    monkeypatch.setattr(install, "_is_source_checkout", lambda: True)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(subprocess, "run", _uv_run([], 1, " boom \n"))

    install._install_cli_tool()

    assert printed == ["  [!!] uv tool install failed: boom"]


@pytest.mark.parametrize(
    ("returncode", "stderr", "message"),
    [
        (0, "", "  [OK] Uninstalled CLI via: uv tool uninstall agentihooks"),
        (2, "error: `agentihooks` is Not Installed", "  [--] agentihooks was not installed via uv tool (skipping)"),
        (2, " disk gone \n", "  [!!] uv tool uninstall failed: disk gone"),
    ],
)
def test_uninstall_runs_uv_then_removes_the_link(wheel_install, monkeypatch, returncode, stderr, message):
    target, link, printed = wheel_install
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    calls = []
    monkeypatch.setattr(install.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(subprocess, "run", _uv_run(calls, returncode, stderr))

    install._uninstall_cli_tool()

    assert calls == [(["/bin/uv", "tool", "uninstall", "agentihooks"], {"capture_output": True, "text": True})]
    assert not link.is_symlink()
    assert printed == [message, f"  [OK] Removed CLI link: {link}"]


def test_uninstall_without_uv_or_link_says_how_to_remove_it(wheel_install, monkeypatch, capsys):
    printed = wheel_install[2]
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    install._uninstall_cli_tool()

    assert printed == ["  [!!] uv not found — cannot uninstall CLI automatically."]
    assert capsys.readouterr().out == "       Remove manually: uv tool uninstall agentihooks\n"


def test_no_link_and_no_uv_means_no_cli(wheel_install, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    assert install._cli_tool_is_installed() is False
