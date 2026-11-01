import threading

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import _tick_all

pytestmark = pytest.mark.unit


def test_host_runs_each_swarm_once_while_another_swarm_is_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    ticks = {"fast": 0, "slow": 0}

    def run_tick(store: object, slug: str) -> list[str]:
        ticks[slug] += 1
        if slug == "slow":
            release.wait(0.15)
        elif ticks[slug] > 1:
            release.set()
        return []

    _tick_all(monkeypatch, run_tick, ["slow", "fast"], tick_seconds=0.01)
    assert ticks == {"fast": 1, "slow": 1}


def test_extra_pass_scheduler_is_retired() -> None:
    assert not hasattr(cli, "_tick_while_others_run")
    assert not hasattr(cli, "EXTRA_TICKS_UNTIL")
    assert not hasattr(cli, "_Firsts")
