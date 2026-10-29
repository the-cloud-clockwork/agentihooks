import threading
from collections import Counter


class LedgerLock:
    def __init__(self) -> None:
        self.state = threading.Condition()
        self.whole = False
        self.waiting = 0
        self.held = Counter()

    def acquire(self, blocking: bool = True) -> bool:
        with self.state:
            if not blocking and (self.whole or self.held):
                return False
            self.waiting += 1
            try:
                self.state.wait_for(lambda: not self.whole and not self.held)
            finally:
                self.waiting -= 1
            self.whole = True
        return True

    def release(self) -> None:
        with self.state:
            if not self.whole:
                raise RuntimeError("release unlocked lock")
            self.whole = False
            self.state.notify_all()

    def locked(self) -> bool:
        with self.state:
            return self.whole or bool(self.held)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    def ledger(self, slug: str) -> "Ledger":
        return Ledger(self, slug)


class Ledger:
    def __init__(self, lock: LedgerLock, slug: str) -> None:
        self.lock, self.slug = lock, slug

    def acquire(self) -> bool:
        state, held = self.lock.state, self.lock.held
        with state:
            state.wait_for(lambda: not self.lock.whole and not self.lock.waiting and not held[self.slug])
            held[self.slug] += 1
        return True

    def release(self) -> None:
        state, held = self.lock.state, self.lock.held
        with state:
            held[self.slug] -= 1
            if not held[self.slug]:
                del held[self.slug]
            state.notify_all()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
