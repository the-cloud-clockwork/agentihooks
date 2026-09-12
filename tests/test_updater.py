from __future__ import annotations

import sys

import pytest

from scripts import updater


@pytest.fixture()
def wheel_install(monkeypatch):
    monkeypatch.setattr(updater, "is_editable", lambda: False)


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


def test_pip_mode_targets_the_running_interpreter(wheel_install, monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "pipx"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))

    assert updater.install_mode() == "pip"
    cmd = updater.upgrade_command("pip")
    assert cmd[0] == sys.executable
    assert "uv" not in cmd


def test_uv_tool_install_is_detected_by_prefix(wheel_install, monkeypatch, tmp_path):
    tools = tmp_path / "uv-tools"
    (tools / "agentihooks").mkdir(parents=True)
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "pipx"))
    monkeypatch.setattr(sys, "prefix", str(tools / "agentihooks"))

    assert updater.install_mode() == "uv-tool"
    assert updater.upgrade_command("uv-tool")[:3] == ["uv", "tool", "install"]


def test_pipx_install_is_detected_by_prefix(wheel_install, monkeypatch, tmp_path):
    pipx = tmp_path / "pipx"
    (pipx / "venvs" / "agentihooks").mkdir(parents=True)
    monkeypatch.setenv("PIPX_HOME", str(pipx))
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    monkeypatch.setattr(sys, "prefix", str(pipx / "venvs" / "agentihooks"))

    assert updater.install_mode() == "pipx"


def test_source_checkout_flag_forces_editable(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "is_editable", lambda: False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))

    assert updater.install_mode(source_checkout=True) == "editable"


def test_up_to_date_runs_no_command(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda source_checkout=False: "pip")
    monkeypatch.setattr(updater, "installed_version", lambda: "2.6.0")
    monkeypatch.setattr(updater, "latest_version", lambda: "2.6.0")
    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: calls.append(a))

    lines: list[str] = []
    assert updater.run_update(echo=lines.append) == 0
    assert calls == []
    assert any("Already up to date" in line for line in lines)


def test_new_release_is_installed_and_reported(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda source_checkout=False: "pip")
    monkeypatch.setattr(updater, "installed_version", lambda: "2.6.0")
    monkeypatch.setattr(updater, "latest_version", lambda: "2.7.0")
    monkeypatch.setattr(updater, "version_after_upgrade", lambda: "2.7.0")
    ran: dict[str, list[str]] = {}

    def _run(cmd, **kwargs):
        ran["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(updater.subprocess, "run", _run)

    lines: list[str] = []
    assert updater.run_update(echo=lines.append) == 0
    assert ran["cmd"][0] == sys.executable
    assert any("Updated: 2.6.0 -> 2.7.0" in line for line in lines)


def test_check_only_installs_nothing(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda source_checkout=False: "pip")
    monkeypatch.setattr(updater, "installed_version", lambda: "2.6.0")
    monkeypatch.setattr(updater, "latest_version", lambda: "2.7.0")
    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: calls.append(a))

    lines: list[str] = []
    assert updater.run_update(check_only=True, echo=lines.append) == 0
    assert calls == []
    assert any("Update available" in line for line in lines)


def test_editable_install_does_not_touch_pypi(monkeypatch):
    monkeypatch.setattr(updater, "is_editable", lambda: True)
    monkeypatch.setattr(updater, "installed_version", lambda: "2.6.0")

    def _boom():
        raise AssertionError("latest_version must not be called for an editable install")

    monkeypatch.setattr(updater, "latest_version", _boom)
    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: calls.append(a))

    lines: list[str] = []
    assert updater.run_update(echo=lines.append) == 0
    assert calls == []
    assert any("Editable install" in line for line in lines)
