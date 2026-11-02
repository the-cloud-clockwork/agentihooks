"""Spend counters a budget gate keeps per subject: gates/<gate>/counts/<subject>/<counter>, one number inside."""

import fcntl
from pathlib import Path

from scripts.gates.log import gates_dir, safe_name


def _number(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


class Budget:
    def __init__(self, slug: str, gate: str, home: str | Path | None = None) -> None:
        self.slug, self.gate, self.home = slug, gate, home

    def path(self, subject: str, counter: str) -> Path:
        return (
            gates_dir(self.slug, self.home) / safe_name(self.gate) / "counts" / safe_name(subject) / safe_name(counter)
        )

    def spent(self, subject: str, counter: str) -> int:
        try:
            return _number(self.path(subject, counter).read_text())
        except OSError:
            return 0

    def spend(self, subject: str, counter: str, cap: int) -> tuple[bool, int]:
        """Count one more spend unless the cap is reached; returns (allowed, spent) under a file lock."""
        path = self.path(subject, counter)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            held.seek(0)
            spent = _number(held.read())
            if spent >= cap:
                return False, spent
            held.seek(0)
            held.truncate()
            held.write(str(spent + 1))
            return True, spent + 1
