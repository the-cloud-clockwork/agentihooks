"""Deadline for the prompt hook's steps."""

import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        keep: tuple[type[BaseException], ...] = (),
    ) -> None:
        self.keep = keep
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

    def run(self, name: str, step: Callable[..., Any], *args: Any) -> Any:
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
        except Spent as cut:
            kept = self._kept(cut)
            if kept is not None:
                raise kept from None
            self.spent = True
            self.cut = name
            return None
        finally:
            self._settle()
            self.running = ""
            self.times[name] = self._clock() - began

    @contextmanager
    def guarded(self, on_overrun: Callable[["PromptBudget"], None] | None = None) -> Iterator["PromptBudget"]:
        installed = self._install()
        watchdog = None
        try:
            if on_overrun is not None:
                watchdog = threading.Timer(max(0.0, self.left(self.hard)) + GRACE_S, on_overrun, (self,))
                watchdog.daemon = True
                watchdog.start()
            yield self
        except Spent as cut:
            kept = self._kept(cut)
            if kept is not None:
                raise kept from None
            self.spent = True
        finally:
            self._settle()
            if watchdog is not None:
                watchdog.cancel()
            if installed is not _UNSET:
                previous, timer = installed
                signal.setitimer(signal.ITIMER_REAL, *timer)
                signal.signal(signal.SIGALRM, previous)
            self.report()

    def report(self) -> None:
        slow = {name: round(took, 2) for name, took in list(self.times.items()) if took >= SLOW_STEP_S}
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

    def _install(self) -> Any:
        try:
            previous = signal.signal(signal.SIGALRM, self._on_alarm)
        except (AttributeError, ValueError):
            return _UNSET
        self._installed = True
        return (signal.SIG_DFL if previous is None else previous, signal.getitimer(signal.ITIMER_REAL))

    def _kept(self, cut: Spent) -> BaseException | None:
        error = cut.__context__
        while error is not None and not isinstance(error, self.keep):
            error = error.__context__
        return error

    def _settle(self) -> None:
        try:
            self._disarm()
        except Spent:
            pass

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
            raise Spent
