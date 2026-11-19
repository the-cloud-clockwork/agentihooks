import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager


class ServeTiming:
    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.running = 0
        self.lock = threading.Lock()

    @contextmanager
    def request(self, subject: str, waited_ms: int) -> Generator[None, None, None]:
        with self.lock:
            self.running += 1
            running = self.running
        try:
            print(
                f"bus: serve subject={subject} waited_ms={waited_ms} running={running} workers={self.workers}",
                file=sys.stderr,
            )
            yield
        finally:
            with self.lock:
                self.running -= 1
