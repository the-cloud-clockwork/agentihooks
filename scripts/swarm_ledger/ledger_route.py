import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from scripts.gates import Who
from scripts.swarm_ledger import ports

CURRENT: ContextVar[tuple[str, str] | None] = ContextVar("ledger_route", default=None)
DEPLOYMENT, LOCAL = "AGENTIHOOKS_DEPLOYMENT", "local"


def configured(store: object, slug: str) -> str:
    if os.environ.get(DEPLOYMENT, LOCAL) != LOCAL:
        return ""
    return store.redis.hget(store.key(slug, "config"), "api_url") or ""


def url(slug: str = "") -> str:
    route = CURRENT.get()
    return route[1] if route and (not slug or route[0] == slug) else ""


@contextmanager
def scoped(slug: str, address: str) -> Iterator[None]:
    token = CURRENT.set((slug, address.rstrip("/")))
    try:
        yield
    finally:
        CURRENT.reset(token)


@contextmanager
def session(slug: str) -> Iterator[None]:
    if (
        os.environ.get("AGENTIHOOKS_SWARM") == slug
        and os.environ.get("AGENTIHOOKS_DEPLOYMENT", "local") == "local"
        and not os.environ.get("SWARM_URL")
    ):
        store = ports.swarm().store.connect()
        with scoped(slug, store.config(slug).api_url):
            yield
            _acknowledge(store, slug, Who.from_env())
    else:
        yield


def _acknowledge(store: object, slug: str, who: Who) -> None:
    if os.environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN"):
        ports.swarm().ledger_cutover.acknowledge(store, slug, who, os.environ)


@contextmanager
def command(store: object, slug: str, who: Who) -> Iterator[None]:
    with scoped(slug, configured(store, slug) if slug else ""):
        if slug:
            _acknowledge(store, slug, who)
        yield


def tick(function: Callable) -> Callable:
    @wraps(function)
    def routed(store, slug, *args, **kwargs):
        with scoped(slug, configured(store, slug)):
            return function(store, slug, *args, **kwargs)

    return routed


def service(function: Callable) -> Callable:
    @tick
    @wraps(function)
    def trusted(store, slug, *args, **kwargs):
        from scripts.swarm_ledger.authority_context import tick_headers

        pending = store.redis.hget(store.key(slug, "config"), "pending_api_url")
        if (url(slug) or pending) and not tick_headers(slug):
            return ["the host tick will apply the settings"]
        return function(store, slug, *args, **kwargs)

    return trusted
