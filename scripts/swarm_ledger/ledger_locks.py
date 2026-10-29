import threading


class LedgerLock:
    def __init__(self) -> None:
        self.state = threading.Condition()
        self.whole = False
        self.waiting = 0
        self.held = set()

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
        lock = self.lock
        with lock.state:
            lock.state.wait_for(lambda: not lock.whole and not lock.waiting and self.slug not in lock.held)
            lock.held.add(self.slug)
        return True

    def release(self) -> None:
        lock = self.lock
        with lock.state:
            lock.held.discard(self.slug)
            lock.state.notify_all()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
