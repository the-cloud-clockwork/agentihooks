"""Shared fixtures for tests/context/* — isolate controls-toggle global state.

The controls-toggle uses a process-wide flag (~/.agentihooks/controls_flags/active.flag
+ redis key controls_disabled:_global). Without isolation, a real session that has
'disable controls' active would make tests that assume bypass-OFF fail (e.g. existing
branch_guard tests that expect blocked force-push).
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_controls_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_dir = tmp_path / "controls_flags"
    monkeypatch.setattr("hooks.context.controls_toggle._FLAG_DIR", flag_dir)
    monkeypatch.setattr("hooks.context.controls_toggle._GLOBAL_FLAG", flag_dir / "active.flag")
    monkeypatch.setattr("hooks.context.controls_toggle.get_redis", lambda: None)
    monkeypatch.setattr("hooks.context.branch_guard.get_redis", lambda: None)
    monkeypatch.setattr("hooks.context.prod_lockdown.get_redis", lambda: None)


@pytest.fixture
def outside_a_guarded_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
