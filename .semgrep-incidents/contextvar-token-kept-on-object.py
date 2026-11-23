from contextvars import ContextVar
from threading import Event

from scripts.swarm import reaper
from scripts.tick.adapters.processes import PassProcesses

PASS: ContextVar[PassProcesses | None] = ContextVar("reaper_pass", default=None)


class TokenSites:
    def start(self) -> None:
        # ruleid: contextvar-token-kept-on-object
        self.scan = reaper.PASS.set(PassProcesses())

    def begin(self) -> None:
        # ruleid: contextvar-token-kept-on-object
        self.token = PASS.set(PassProcesses())

    def close(self) -> None:
        reaper.PASS.reset(self.scan)


class ValueSites:
    def start(self) -> None:
        self.prior = reaper.PASS.get()
        self.scan = PassProcesses()
        # ok: contextvar-token-kept-on-object
        reaper.PASS.set(self.scan)

    def close(self) -> None:
        if reaper.PASS.get() is self.scan:
            # ok: contextvar-token-kept-on-object
            reaper.PASS.set(self.prior)

    def signal(self, ready: Event) -> None:
        # ok: contextvar-token-kept-on-object
        self.flag = ready.set()


def scanned() -> None:
    # ok: contextvar-token-kept-on-object
    token = reaper.PASS.set(PassProcesses())
    try:
        reaper.PASS.get()
    finally:
        reaper.PASS.reset(token)
