# ruleid: feature-tick-database-ledger
def routed(store, slug, *args, **kwargs):
    with scoped(slug, configured(store, slug)), database_ledger.scoped():
        return function(store, slug, *args, **kwargs)


# ok: feature-tick-database-ledger
def routed(store: object, slug: str, *args: object, **kwargs: object) -> object:
    from scripts.tick.adapters import database_ledger

    with database_ledger.acting(""):
        return function(store, slug, *args, **kwargs)


# ruleid: feature-tick-database-ledger
def build(remaining: float) -> ResourceClient:
    base: str = ledger.base(slug)
    timeout: float = min(ledger.REQUEST_TIMEOUT, remaining)
    if database_ledger.serving():
        return database_ledger.client(base, timeout)
    # ruleid: feature-tick-database-ledger
    return ResourceClient(base, ledger.credentials(slug, service), timeout)


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


# ruleid: feature-tick-database-ledger
def client(slug, service=False, timeout=REQUEST_TIMEOUT, headers=None):
    from scripts.tick.adapters import database_ledger

    if database_ledger.serving():
        return database_ledger.client(base(slug), timeout)
    return ResourceClient(base(slug), credentials(slug, service) if headers is None else headers, timeout)


# ruleid: feature-tick-database-ledger
def client(slug: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    return ResourceClient(base(slug), credentials(slug), timeout)


# ok: feature-tick-database-ledger
def client(slug: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    """The ledger API client for slug: the ledger API route code run in this process over the shared database."""
    from scripts.tick.adapters import database_ledger

    return database_ledger.client(base(slug), timeout)


# ok: feature-tick-database-ledger
def client(slug: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    """The ledger API client for slug: this host's warm writer first, else the ledger API route code in process."""
    from scripts.tick.adapters import writer_client

    return writer_client.client(base(slug), timeout)
