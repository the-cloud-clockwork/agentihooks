"""An extra swarm tick pass starts only when it can finish by the extra pass cutoff."""

import threading
import types
from typing import Any, cast

import pytest

from scripts.swarm import cli

pytestmark = pytest.mark.unit


def _clocked(monkeypatch: pytest.MonkeyPatch, pass_s: float) -> list[float]:
    now = [0.0]
    starts: list[float] = []

    def tick_one(store: object, slug: str) -> None:
        starts.append(now[0])
        now[0] += pass_s

    monkeypatch.setattr(cli, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(cli, "_tick_one", tick_one)
    monkeypatch.setattr(cli, "TICK_SECONDS", 0)
    return starts


class _Unsettled:
    def __init__(self, rounds: int) -> None:
        self.left = rounds

    def wait(self, timeout: float) -> bool:
        self.left -= 1
        return self.left < 0

    def set(self) -> None:
        return None


def _firsts(rounds: int) -> cli._Firsts:
    firsts = cast(Any, cli._Firsts(1))
    firsts.settled = _Unsettled(rounds)
    firsts.lock = threading.Lock()
    return cast(cli._Firsts, firsts)


def test_no_extra_pass_starts_when_the_longest_pass_would_end_past_the_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    starts = _clocked(monkeypatch, 120.0)
    cli._tick_while_others_run(cast(Any, None), "sw", _firsts(5), 200.0)
    assert starts == [0.0]


def test_extra_passes_run_while_the_longest_pass_still_ends_by_the_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    starts = _clocked(monkeypatch, 100.0)
    cli._tick_while_others_run(cast(Any, None), "sw", _firsts(5), 400.0)
    assert starts == [0.0, 100.0, 200.0, 300.0]
