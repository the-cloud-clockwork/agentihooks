import math
import threading
from collections.abc import Callable

STOP_SECONDS = 15


class Interval:
    def __init__(self, callback: Callable[[], None], *, every: float) -> None:
        if not math.isfinite(every) or every <= 0:
            raise ValueError("an interval must be positive and finite")
        self.callback, self.every = callback, every
        self._stop = threading.Event()
        self._error = None
        self._worker = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._worker.start()

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.every):
                self.callback()
        except Exception as error:
            self._error = error

    def check(self) -> None:
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=STOP_SECONDS)
        if self._worker.is_alive():
            raise RuntimeError("scheduled interval did not stop")
        self.check()
