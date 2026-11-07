"""Grace window that keeps the controller alive through a Redis outage shorter than it."""

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

GRACE_ENV = "AGENTIHOOKS_CONTROLLER_REDIS_GRACE_S"
DEFAULT_GRACE_S = 120.0
MARKER = Path("controller") / "redis-outage"


def grace_seconds(environ: Mapping[str, str]) -> float:
    """Seconds of Redis refusal the controller rides out before its liveness fails."""
    return float(environ.get(GRACE_ENV) or DEFAULT_GRACE_S)


def marker(environ: Mapping[str, str]) -> Path:
    """File that keeps the wall clock time of the first failed probe of the current outage."""
    home = environ.get("AGENTIHOOKS_HOME") or str(Path.home() / ".agentihooks")
    return Path(home) / MARKER


def down_for(path: Path, now: float) -> float:
    """Seconds since the outage's first failed probe, recording now as its start when none is kept."""
    try:
        return now - float(path.read_text())
    except (OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(now))
        return 0.0


def cleared(path: Path) -> None:
    """End the recorded outage."""
    path.unlink(missing_ok=True)


class Outage:
    """One in-process Redis outage measured on a monotonic clock."""

    def __init__(self, grace_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.grace_s = grace_s
        self.clock = clock
        self.since: float | None = None

    @classmethod
    def from_environ(cls) -> "Outage":
        """Outage whose window is the grace setting of the process environment."""
        return cls(grace_seconds(os.environ))

    def tolerated(self) -> bool:
        """Record a Redis failure and answer whether the outage is still inside the window."""
        now = self.clock()
        if self.since is None:
            self.since = now
        return now - self.since < self.grace_s

    def ended(self) -> None:
        """Mark Redis as answering again."""
        self.since = None
