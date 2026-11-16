from collections.abc import Callable
from functools import wraps


def tick(function: Callable) -> Callable:
    """The call is judged as the swarm's own service."""

    @wraps(function)
    def routed(store: object, slug: str, *args: object, **kwargs: object) -> object:
        from scripts.tick.adapters import database_ledger

        with database_ledger.acting(""):
            return function(store, slug, *args, **kwargs)

    return routed
