import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import _count

BACKEND_ENV = "AGENTIHOOKS_BUS"
URL_ENV = "AGENTIHOOKS_BUS_URL"
CREDENTIALS_ENV = "AGENTIHOOKS_BUS_CREDENTIALS"
BIND_ENV = "AGENTIHOOKS_BUS_BIND"
SCOPED_ENV = "AGENTIHOOKS_BUS_SCOPED"
OPENBAO_SCHEME = "openbao:"
CREDENTIALS_FIELD = "credentials"
DEFAULT_BACKEND = "memory"
SHARED_SLOT_BUCKETS = ("account-slots", "hive-slot-accounts")
SCOPED_STATE_BUCKETS = (
    "spoke-work",
    "spoke-pending",
    "hive-presence",
    "hive-executions",
    "execution-starts",
    "account-slots",
    "hive-slot-accounts",
)


def _positive(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class QueuePolicy:
    max_deliver: int = 5
    ack_wait_ms: int = 30_000
    backoff_ms: tuple[int, ...] = ()
    inactive_ms: int | None = None

    def __post_init__(self):
        if not _positive(self.max_deliver):
            raise ValueError("queue max deliver must be a positive integer")
        if not _positive(self.ack_wait_ms):
            raise ValueError("queue ack wait must be a positive integer")
        if not isinstance(self.backoff_ms, tuple) or not all(map(_count, self.backoff_ms)):
            raise ValueError("queue backoff must be a tuple of non negative integers")
        if len(self.backoff_ms) > self.max_deliver:
            raise ValueError(f"queue backoff has {len(self.backoff_ms)} steps for {self.max_deliver} deliveries")
        if self.inactive_ms is not None and not _positive(self.inactive_ms):
            raise ValueError("queue inactive threshold must be a positive integer")


@dataclass(frozen=True)
class BusConfig:
    backend: str = DEFAULT_BACKEND
    url: str | None = None
    dedupe_window_ms: int = 120_000
    queue: QueuePolicy = field(default_factory=QueuePolicy)
    credentials: str | None = field(default=None, repr=False)
    provision: bool = True
    scoped: bool = False

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
        return cls(
            backend=environ.get(BACKEND_ENV) or DEFAULT_BACKEND,
            url=environ.get(URL_ENV) or None,
            credentials=credentials_from(environ),
            provision=environ.get(BIND_ENV) != "1",
            scoped=environ.get(SCOPED_ENV) == "1",
        )


def credentials_from(environ: Mapping[str, str]) -> str | None:
    reference = environ.get(CREDENTIALS_ENV)
    if not reference:
        return None
    if not reference.startswith(OPENBAO_SCHEME):
        return Path(reference).read_text()
    from scripts import openbao_kv

    bao = openbao_kv.from_env(environ)
    if bao is None:
        raise ValueError(
            f"{CREDENTIALS_ENV} names an OpenBao record, so {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must be set"
        )
    record, _ = openbao_kv.read(bao, reference.removeprefix(OPENBAO_SCHEME))
    if not record.get(CREDENTIALS_FIELD):
        raise ValueError(f"the OpenBao record {reference} holds no {CREDENTIALS_FIELD}")
    return record[CREDENTIALS_FIELD]
