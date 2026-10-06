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
