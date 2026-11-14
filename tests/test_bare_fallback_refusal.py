"""Regression guards for the bare Claude fallback refusing a non master profile."""

from pathlib import Path
from typing import Any

import pytest

from scripts import install
from scripts.claude_quota_balancer import RoutingError
from scripts.hive import account_slots


def _unroutable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executed: list[str]) -> None:
    """Make every route refuse and record any exec instead of running it."""

    def refused(*args: Any, **kwargs: Any) -> None:
        raise RoutingError("no router accounts")

    def admitted(harness: str, account: str, env: dict[str, str], invoke: Any) -> Any:
        return invoke(env)

    def execvpe(binary: str, argv: list[str], env: dict[str, str]) -> None:
        executed.append(binary)
        raise RuntimeError("exec reached")

    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda env: tmp_path / "quota.json")
    monkeypatch.setattr("scripts.claude_quota_balancer.route_requires_fable", lambda *args: False)
    monkeypatch.setattr("scripts.claude_quota_balancer.select_credential", refused)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/opt/claude")
    monkeypatch.setattr(account_slots, "execute", admitted)
    monkeypatch.setattr(install.os, "execvpe", execvpe)


@pytest.mark.feature("bare-fallback-worker-refuse")
@pytest.mark.parametrize("profile", ["engineer", "cicd", "planner"])
def test_a_worker_launch_with_no_routed_account_fails_with_a_route_report(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executed: list[str] = []
    _unroutable(monkeypatch, tmp_path, executed)
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", profile)
    report = tmp_path / "launcher.route"

    with pytest.raises(SystemExit) as exc:
        install.cmd_claude(["--agentihooks-fallback-bare", "--agentihooks-report", str(report)])

    assert exc.value.code == 3
    assert executed == []
    text = report.read_text(encoding="utf-8")
    assert "status=failed\n" in text
    assert f"bare Claude is refused for profile {profile}" in text
    assert "launching bare Claude" not in capsys.readouterr().err


@pytest.mark.feature("bare-fallback-worker-refuse")
@pytest.mark.parametrize("profile", ["master", ""])
def test_a_master_bare_launch_is_unchanged(profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []
    _unroutable(monkeypatch, tmp_path, executed)
    if profile:
        monkeypatch.setenv("AGENTIHOOKS_PROFILE", profile)
    report = tmp_path / "launcher.route"

    with pytest.raises(RuntimeError, match="^exec reached$"):
        install.cmd_claude(["--agentihooks-fallback-bare", "--agentihooks-report", str(report)])

    assert executed == ["/opt/claude"]
    assert "status=bare\n" in report.read_text(encoding="utf-8")


@pytest.mark.feature("bare-fallback-worker-refuse")
def test_a_launch_without_the_fallback_fails_even_on_an_empty_route_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[str] = []
    _unroutable(monkeypatch, tmp_path, executed)

    def silent(*args: Any, **kwargs: Any) -> None:
        raise RoutingError("")

    monkeypatch.setattr("scripts.claude_quota_balancer.select_credential", silent)
    report = tmp_path / "launcher.route"

    with pytest.raises(SystemExit) as exc:
        install.cmd_claude(["--agentihooks-report", str(report)])

    assert exc.value.code == 3
    assert executed == []
    assert "status=failed\n" in report.read_text(encoding="utf-8")
