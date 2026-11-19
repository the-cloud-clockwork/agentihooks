from typing import Any, cast

from scripts.swarm_ledger.ledger import resource

COLLECTIONS = ("tasks", "freezes", "plans", "phases", "followups", "alerts")


def read(slug: str) -> dict[str, list[dict[str, Any]]]:
    return {name: cast(list[dict[str, Any]], resource(slug, name, collection=True)) for name in COLLECTIONS}
