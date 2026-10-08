from collections.abc import Sequence
from typing import Literal

from scripts.routing.slots import Slot

Side = Literal["api", "pool"]


def choose_side(
    api: Sequence[Slot], pool: Sequence[Slot], weight: int, api_live: int, pool_live: int, api_room: bool
) -> Side | None:
    if not api:
        return "pool"
    if not pool:
        return "api" if api_room else None
    if api_live / (api_live + pool_live + 1) < weight / 100 and api_room:
        return "api"
    return "pool"
