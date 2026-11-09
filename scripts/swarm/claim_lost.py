"""A worker has lost its task when the ledger names another claimant, or names none while the worker runs as a hive
member: the tick restores an emptied claim only for a live workstation agent."""


def lost(name: str, row: dict | None, hive: bool) -> bool:
    if not row:
        return False
    owner = row.get("claimed_by") or ""
    return owner != name and (bool(owner) or hive)
