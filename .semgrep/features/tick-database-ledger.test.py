# ruleid: feature-tick-database-ledger
def routed(store, slug, *args, **kwargs):
    with scoped(slug, configured(store, slug)):
        return function(store, slug, *args, **kwargs)


# ok: feature-tick-database-ledger
def routed(store, slug, *args, **kwargs):
    from scripts.tick.adapters import database_ledger

    with scoped(slug, configured(store, slug)), database_ledger.scoped():
        return function(store, slug, *args, **kwargs)


# ruleid: feature-tick-database-ledger
def build(remaining: float) -> ResourceClient:
    base: str = ledger.base(slug)
    timeout: float = min(ledger.REQUEST_TIMEOUT, remaining)
    return ResourceClient(base, ledger.credentials(slug, service), timeout)


# ok: feature-tick-database-ledger
def build(remaining: float) -> ResourceClient:
    base: str = ledger.base(slug)
    timeout: float = min(ledger.REQUEST_TIMEOUT, remaining)
    if database_ledger.serving():
        return database_ledger.DatabaseResourceClient(base, {}, timeout)
    return ResourceClient(base, ledger.credentials(slug, service), timeout)
