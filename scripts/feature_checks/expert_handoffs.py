"""Live check of the expert handoffs: in every swarm that is not stopped, the handoff an expert left on the seat of a
running lineage conforms to the expert handoff contract against the saved mission of that lineage."""

import sys
from collections.abc import Iterable
from typing import Any, Protocol


class Handoffs(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state, its keys and its stored handoffs."""

    redis: Any

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def key(self, slug: str, *parts: str) -> str: ...

    def handoff(self, slug: str, task: str) -> str: ...


def main(store: Handoffs | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
