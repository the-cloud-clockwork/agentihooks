import os
from collections.abc import Mapping
from dataclasses import dataclass, field

BACKEND_ENV = "AGENTIHOOKS_BUS"
URL_ENV = "AGENTIHOOKS_BUS_URL"
DEFAULT_BACKEND = "memory"


def _positive(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class QueuePolicy:
    max_deliver: int = 5
    ack_wait_ms: int = 30_000
    backoff_ms: tuple[int, ...] = ()

    def __post_init__(self):
        if not _positive(self.max_deliver):
            raise ValueError("queue max deliver must be a positive integer")
        if not _positive(self.ack_wait_ms):
            raise ValueError("queue ack wait must be a positive integer")
        if not isinstance(self.backoff_ms, tuple) or not all(map(_count, self.backoff_ms)):
            raise ValueError("queue backoff must be a tuple of non negative integers")
        if len(self.backoff_ms) > self.max_deliver:
            raise ValueError(f"queue backoff has {len(self.backoff_ms)} steps for {self.max_deliver} deliveries")


@dataclass(frozen=True)
class BusConfig:
    backend: str = DEFAULT_BACKEND
    url: str | None = None
    dedupe_window_ms: int = 120_000
    queue: QueuePolicy = field(default_factory=QueuePolicy)

    def __post_init__(self):
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("bus backend must be non empty text")
        if self.url is not None and (not isinstance(self.url, str) or not self.url):
            raise ValueError("bus url must be non empty text or None")
        if not _positive(self.dedupe_window_ms):
            raise ValueError("bus dedupe window must be a positive integer")
        if not isinstance(self.queue, QueuePolicy):
            raise ValueError("bus queue must be a QueuePolicy")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] = os.environ) -> "BusConfig":
        return cls(backend=environ.get(BACKEND_ENV) or DEFAULT_BACKEND, url=environ.get(URL_ENV) or None)
