from typing import Any

from scripts.hub import github_webhooks
from scripts.hub.github_webhooks import LazyPulls


def scoped_only(bus: Any) -> Any:
    # ruleid: receiver-pulls-without-unscoped-index
    return LazyPulls(bus)


def scoped_only_by_module(bus: Any) -> Any:
    # ruleid: receiver-pulls-without-unscoped-index
    return github_webhooks.LazyPulls(bus)


def with_index(bus: Any, index: Any) -> Any:
    # ok: receiver-pulls-without-unscoped-index
    return LazyPulls(bus, index)


def with_named_index(bus: Any, index: Any) -> Any:
    # ok: receiver-pulls-without-unscoped-index
    return github_webhooks.LazyPulls(bus, index=index)
