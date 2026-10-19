"""Deadline for the prompt hook's steps, so a slow step costs its own output and never the blocks already injected."""

import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from hooks.common import log

SOFT_S = float(os.getenv("AGENTIHOOKS_PROMPT_BUDGET_S", "10"))
HARD_S = float(os.getenv("AGENTIHOOKS_PROMPT_HARD_BUDGET_S", "20"))
SLOW_STEP_S = 1.0


class Spent(BaseException):
    """BaseException so a step's own ``except Exception`` cannot swallow the cut."""


def process_start(clock=time.monotonic) -> float:
    """The monotonic instant this process started, so the budget also counts interpreter start and imports."""
    try:
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        age = 0.0
    return clock() - max(0.0, age)


class PromptBudget:
    def __init__(self, start=None, soft=None, hard=None, clock=time.monotonic):
        self._clock = clock
        self._start = clock() if start is None else start
        self.soft = SOFT_S if soft is None else soft
        self.hard = HARD_S if hard is None else hard
        self.spent = False
        self.cut = ""
        self.skipped: list[str] = []
        self.times: dict[str, float] = {}
        self._armed = False

    def left(self, limit: float) -> float:
        return self._start + limit - self._clock()

    def run(self, name, step, *args):
        if self.spent or self.left(self.soft) <= 0:
            self.spent = True
            self.skipped.append(name)
            return None
        began = self._clock()
        try:
            self._arm(self.left(self.hard))
            return step(*args)
        except Spent:
            self.spent = True
            self.cut = name
            return None
        finally:
            self._disarm()
            self.times[name] = self._clock() - began

    @contextmanager
    def guarded(self):
        try:
            previous = signal.signal(signal.SIGALRM, self._on_alarm)
        except (AttributeError, ValueError):
            previous = None
        try:
            yield self
        finally:
            if previous is not None:
                signal.setitimer(signal.ITIMER_REAL, 0)
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

    def _arm(self, seconds: float) -> None:
        if signal.getsignal(signal.SIGALRM) != self._on_alarm:
            return
        if seconds <= 0:
            raise Spent
        self._armed = True
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def _disarm(self) -> None:
        if self._armed:
            self._armed = False
            signal.setitimer(signal.ITIMER_REAL, 0)

    def _on_alarm(self, signum, frame):
        if self._armed:
            self._armed = False
            raise Spent
