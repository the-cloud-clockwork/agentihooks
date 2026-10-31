"""Explicit CI fault probe; excluded from ordinary test discovery."""

from pathlib import Path

import pytest

from tests.swarm.tick_scenarios.test_characterisation import test_dropped_reap_action_breaks_golden as check_drop


@pytest.mark.parametrize("entry", ["tick", "run_tick"])
def test_planted_reap_action_is_red(entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TICK_CHARACTERISATION_PLANT", "1")
    check_drop(entry, monkeypatch, tmp_path)
