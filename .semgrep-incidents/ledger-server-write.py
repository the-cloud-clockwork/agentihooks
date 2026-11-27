from scripts.tick.adapters import database_ledger


def switched(slug: str) -> object:
    # ruleid: ledger-write-switch
    if database_ledger.serving():
        return database_ledger.client(slug, 10)
    return None


def scoped_pass(slug: str) -> None:
    # ruleid: ledger-write-switch
    with database_ledger.scoped():
        return None


def acted(slug: str) -> None:
    # ok: ledger-write-switch
    with database_ledger.acting(""):
        return None
