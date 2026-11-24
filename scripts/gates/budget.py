"""Spend counters a budget gate keeps per subject: gates/<gate>/counts/<subject>/<counter>, one number inside, and the
spends each call made: gates/<gate>/calls/<call>, one subject and counter per line."""

import fcntl
from pathlib import Path

from scripts.gates.log import gates_dir, safe_name


def _number(text):
    try:
        return int(text)
    except ValueError:
        return 0


class Budget:
    def __init__(self, slug, gate, home=None):
        self.slug, self.gate, self.home = slug, gate, home

    def path(self, subject, counter):
        return (
            gates_dir(self.slug, self.home) / safe_name(self.gate) / "counts" / safe_name(subject) / safe_name(counter)
        )

    def call_path(self, call: str) -> Path:
        return gates_dir(self.slug, self.home) / safe_name(self.gate) / "calls" / safe_name(call)

    def spent(self, subject, counter):
        try:
            return _number(self.path(subject, counter).read_text())
        except OSError:
            return 0

    def spend(self, subject: str, counter: str, cap: int, call: str = "") -> tuple[bool, int]:
        """Count one more spend unless the cap is reached; returns (allowed, spent) under a file lock. A spend made for a
        call is listed under it, so refund can give it back when the call never runs."""
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
            if call:
                listed = self.call_path(call)
                listed.parent.mkdir(exist_ok=True)
                with listed.open("a") as calls:
                    calls.write(f"{subject}\t{counter}\n")
            return True, spent + 1

    def refund(self, call: str) -> None:
        """Give back once each spend listed under call, never below zero."""
        listed = self.call_path(call)
        try:
            spends = listed.read_text().splitlines()
            listed.unlink()
            for spend in spends:
                subject, _, counter = spend.rpartition("\t")
                with self.path(subject, counter).open("a+") as held:
                    fcntl.flock(held, fcntl.LOCK_EX)
                    held.seek(0)
                    spent = _number(held.read())
                    held.seek(0)
                    held.truncate()
                    held.write(str(max(spent - 1, 0)))
        except OSError:
            return
