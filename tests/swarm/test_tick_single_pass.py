import threading

import pytest

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

    _tick_all(monkeypatch, run_tick, ["slow", "fast"])
    assert ticks == {"fast": 1, "slow": 1}
