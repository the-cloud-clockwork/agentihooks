"""The thread that runs held ledger write side effects outside the write request."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread

CLOSE_S = 5.0


class Effects:
    """Runs the side effects ledger writes left held, on its own thread, each time a write or the watch wakes it."""

    def __init__(self, drain: Callable[[], object]) -> None:
        self.drain = drain
        self.due = Event()
        self.stopping = False
        self.thread = Thread(target=self.run, name="ledger-side-effects", daemon=True)

    def wake(self) -> None:
        self.due.set()

    def run(self) -> None:
        while True:
            self.due.wait()
            self.due.clear()
            if self.stopping:
                return
            try:
                self.drain()
            except Exception as exc:
                sys.stderr.write(f"ledger side effects: {exc}\n")

    def close(self) -> None:
        self.stopping = True
        self.due.set()
        self.thread.join(CLOSE_S)


@contextmanager
def effects(drain: Callable[[], object]) -> Iterator[Effects]:
    worker = Effects(drain)
    worker.thread.start()
    try:
        yield worker
    finally:
        worker.close()
