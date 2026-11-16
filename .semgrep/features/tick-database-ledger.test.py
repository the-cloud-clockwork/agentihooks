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
    # ruleid: feature-tick-database-ledger
    return ResourceClient(base, ledger.credentials(slug, service), timeout)


# ok: feature-tick-database-ledger
def build(remaining: float) -> ResourceClient:
    base: str = ledger.base(slug)
    timeout: float = min(ledger.REQUEST_TIMEOUT, remaining)
    if database_ledger.serving():
        return database_ledger.client(base, timeout)
    return ResourceClient(base, ledger.credentials(slug, service), timeout)


# ok: feature-tick-database-ledger
def routed(store: object, slug: str, *args: object, **kwargs: object) -> object:
    from scripts.tick.adapters import database_ledger

    with database_ledger.acting(""):
        return function(store, slug, *args, **kwargs)


# ok: feature-tick-database-ledger
def build(remaining: float) -> ResourceClient:
    if offline():
        raise LedgerOffline(f"ledger {slug}: {OFFLINE_READ}")
    built: ResourceClient = ledger.client(slug, min(ledger.REQUEST_TIMEOUT, remaining))
    return built


def build(remaining: float) -> ResourceClient:
    # ruleid: feature-tick-database-ledger
    spare: ResourceClient = ResourceClient(ledger.base(slug), {}, remaining)
    built: ResourceClient = ledger.client(slug, min(ledger.REQUEST_TIMEOUT, remaining))
    return built


def build(remaining: float) -> ResourceClient:
    base: str = ledger.base(slug)
    # ruleid: feature-tick-database-ledger
    spare: ResourceClient = ResourceClient(base, {}, remaining)
    if database_ledger.serving():
        return database_ledger.client(base, remaining)
    return ResourceClient(base, ledger.credentials(slug, service), remaining)


# ruleid: feature-tick-database-ledger
def client(slug: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    return ResourceClient(base(slug), credentials(slug), timeout)


# ok: feature-tick-database-ledger
def client(slug: str, service: bool = False, timeout: float = REQUEST_TIMEOUT) -> Any:
    from scripts.tick.adapters import database_ledger

    if database_ledger.serving():
        return database_ledger.client(base(slug), timeout)
    return ResourceClient(base(slug), credentials(slug, service), timeout)


# ok: feature-tick-database-ledger
def client(slug: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    from scripts.tick.adapters import database_ledger

    return database_ledger.client(base(slug), timeout)
