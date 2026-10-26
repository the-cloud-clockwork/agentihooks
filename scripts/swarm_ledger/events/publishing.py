import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm_ledger.repository.entry import Entry


class Publishing:
    """A LedgerRepository that hands each stored generation it reads or writes to every publisher(slug, state) once,
    on its own thread, newest generation per ledger, so no reader or writer waits for a publication."""

    def __init__(self, inner, *publishers):
        self.inner = inner
        self.publishers = list(publishers)
        self.published = {}
        self.queued = threading.Condition()
        self.pending = {}
        self.worker = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def publish(self, slug, state):
        for publisher in self.publishers:
            publisher(slug, state)

    def current(self, slug: str) -> "Entry":
        entry = self.inner.current(slug)
        if entry.generation > self.published.get(slug, -1):
            self.queue(slug, entry)
        return entry

    def queue(self, slug: str, entry: "Entry") -> None:
        with self.queued:
            held = self.pending.get(slug)
            if held is None or held.generation < entry.generation:
                self.pending[slug] = entry
            if self.worker is None:
                self.worker = threading.Thread(target=self.run, name="ledger-publish", daemon=True)
                self.worker.start()
            self.queued.notify_all()

    def run(self) -> None:
        while True:
            with self.queued:
                self.queued.wait_for(lambda: self.pending)
                slug, entry = next(iter(self.pending.items()))
            try:
                if entry.generation > self.published.get(slug, -1):
                    self.publish(slug, entry.state)
                    self.published[slug] = entry.generation
            except Exception as exc:  # the next read of this ledger queues the generation again
                sys.stderr.write(f"stream publish for {slug} generation {entry.generation}: {exc}\n")
            finally:
                with self.queued:
                    held = self.pending.pop(slug)
                    if held is not entry:
                        self.pending[slug] = held
                    self.queued.notify_all()

    def drain(self, timeout: float) -> None:
        with self.queued:
            if not self.queued.wait_for(lambda: not self.pending, timeout):
                raise TimeoutError(f"stream publications still pending after {timeout} s")

    def get_document(self, slug: str) -> dict:
        return self.current(slug).mutable()

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None, *, copy_state: bool = True
    ) -> tuple[dict, list]:
        options = {} if copy_state else {"copy_state": False}
        state, rejected = self.inner.apply_ops(slug, changes=changes, ops=ops, gate=gate, **options)
        self.current(slug)
        return state, rejected


SHARED = {}


def publishing(inner, publisher):
    """The one wrapper of a repository, shared by every module copy of the server that adds its publisher."""
    wrapper = SHARED.setdefault(id(inner), Publishing(inner))
    if publisher not in wrapper.publishers:
        wrapper.publishers.append(publisher)
    return wrapper
