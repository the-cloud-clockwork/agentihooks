from typing import Any

from scripts.swarm import claim_lost


def permissive_owner(row: dict[str, Any], name: str) -> bool:
    # ruleid: empty-claim-keeps-worker
    return row.get("claimed_by", "") in ("", name)


def permissive_hook(owner: str | None, name: str) -> str | None:
    # ruleid: empty-claim-keeps-worker
    if not owner or owner == name:
        return None
    return "exit"


def checked_owner(row: dict[str, Any], name: str) -> bool:
    # ok: empty-claim-keeps-worker
    return not claim_lost.lost(name, row, True)


def checked_hook(owner: str | None, name: str) -> str | None:
    # ok: empty-claim-keeps-worker
    if owner is None or not claim_lost.lost(name, {"claimed_by": owner}, True):
        return None
    return "exit"
