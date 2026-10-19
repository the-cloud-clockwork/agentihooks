"""Deadline for the prompt hook's steps, so a slow step costs its own output and never the blocks already injected."""

import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from hooks.common import log

SOFT_S = 10.0
HARD_S = 20.0
GRACE_S = 5.0
SLOW_STEP_S = 1.0
_UNSET = object()


class Spent(BaseException):
    """BaseException so a step's own ``except Exception`` cannot swallow the cut."""


def seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def process_start(clock: Callable[[], float] = time.monotonic) -> float:
    try:
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        age = 0.0
    return clock() - max(0.0, age)


class PromptBudget:
    def __init__(
        self,
        start: float | None = None,
        soft: float | None = None,
        hard: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._start = clock() if start is None else start
        self.soft = seconds("AGENTIHOOKS_PROMPT_BUDGET_S", SOFT_S) if soft is None else soft
        self.hard = seconds("AGENTIHOOKS_PROMPT_HARD_BUDGET_S", HARD_S) if hard is None else hard
        self.spent = False
        self.cut = ""
        self.skipped: list[str] = []
        self.times: dict[str, float] = {}
        self.running = ""
        self._armed = False
        self._installed = False

    def left(self, limit: float) -> float:
        return self._start + limit - self._clock()

    def run(self, name: str, step: Callable, *args):
        if self.spent or self.left(self.soft) <= 0 or (self._installed and self.left(self.hard) <= 0):
            self.spent = True
            self.skipped.append(name)
            return None
        began = self._clock()
        self.running = name
        try:
            self._arm(self.left(self.hard))
            result = step(*args)
            self._disarm()
            return result
        except Spent:
            self.spent = True
            self.cut = name
            return None
        finally:
            self._disarm()
            self.running = ""
            self.times[name] = self._clock() - began

    @contextmanager
    def guarded(self, on_overrun: Callable[["PromptBudget"], None] | None = None) -> Iterator["PromptBudget"]:
        previous, timer = self._install(), self._saved_timer()
        watchdog = None
        try:
            if on_overrun is not None:
                watchdog = threading.Timer(max(0.0, self.left(self.hard)) + GRACE_S, on_overrun, (self,))
                watchdog.daemon = True
                watchdog.start()
            yield self
        except Spent:
            self.spent = True
        finally:
            self._disarm()
            if watchdog is not None:
                watchdog.cancel()
            if previous is not _UNSET:
                signal.setitimer(signal.ITIMER_REAL, *timer)
                signal.signal(signal.SIGALRM, previous)
            self.report()

    def report(self) -> None:
        slow = {name: round(took, 2) for name, took in self.times.items() if took >= SLOW_STEP_S}
        if self.skipped or self.cut or slow:
            log(
                "prompt hook budget",
                {
                    "elapsed_s": round(self.hard - self.left(self.hard), 2),
                    "cut": self.cut,
                    "skipped": self.skipped,
                    "slow": slow,
                },
            )

    def _install(self):
        try:
            previous = signal.signal(signal.SIGALRM, self._on_alarm)
        except (AttributeError, ValueError):
            return _UNSET
        self._installed = True
        return signal.SIG_DFL if previous is None else previous

    def _saved_timer(self) -> tuple[float, float]:
        if not self._installed:
            return (0.0, 0.0)
        return signal.getitimer(signal.ITIMER_REAL)

    def _arm(self, seconds_left: float) -> None:
        if self._installed:
            self._armed = True
            signal.setitimer(signal.ITIMER_REAL, max(seconds_left, 0.001))

    def _disarm(self) -> None:
        if self._armed:
            self._armed = False
            signal.setitimer(signal.ITIMER_REAL, 0)

    def _on_alarm(self, signum, frame) -> None:
        if self._armed:
            self._armed = False
            raise Spent
