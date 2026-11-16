from collections.abc import Callable
from typing import Any


class PollingLock:
    def take(self) -> bool:
        return False

    def lead(self, wait: Callable[[float], None], poll_s: float = 5.0) -> None:
        # ruleid: replica-standby-sleeps-past-lock-lapse
        while not self.take():
            wait(poll_s)


class LapseAwareLock:
    redis: Any
    key: str

    def take(self) -> bool:
        return False

    def lead(self, wait: Callable[[float], None], poll_s: float = 5.0) -> None:
        # ok: replica-standby-sleeps-past-lock-lapse
        while not self.take():
            left_ms = self.redis.pttl(self.key)
            if left_ms != -2:
                wait(poll_s if left_ms < 0 else min(poll_s, left_ms / 1000))
