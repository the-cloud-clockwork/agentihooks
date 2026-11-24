from collections.abc import Callable
from functools import partial

from scripts.tick.view import LedgerView, _kept


def start(
    refresh: Callable[..., LedgerView],
) -> tuple[Callable[[], LedgerView], ...]:
    # ruleid: tick-view-renew-stores-whole-view
    bare = _kept(refresh)
    # ruleid: tick-view-renew-stores-whole-view
    omitted = _kept(partial(refresh))
    # ruleid: tick-view-renew-stores-whole-view
    stores = _kept(partial(refresh, renew=False))
    # ok: tick-view-renew-stores-whole-view
    renew = _kept(partial(refresh, renew=True))
    return bare, omitted, stores, renew
