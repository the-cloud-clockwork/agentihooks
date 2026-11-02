from collections.abc import Callable
from time import monotonic

__all__ = ["Deadline"]


class Deadline:
    def __init__(self, expires_at: float, clock: Callable[[], float] = monotonic) -> None:
        self.expires_at = expires_at
        self.clock = clock

    @classmethod
    def after(cls, seconds: float, *, clock: Callable[[], float] = monotonic) -> "Deadline":
        return cls(clock() + seconds, clock)

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    @property
    def expired(self) -> bool:
        return self.clock() >= self.expires_at

    def child(self, seconds: float) -> "Deadline":
        return Deadline(min(self.expires_at, self.clock() + seconds), self.clock)

    def live(self) -> float:
        """The seconds left, refusing a deadline that has already expired."""
        if self.expired:
            raise TimeoutError("tool deadline expired")
        return self.remaining
