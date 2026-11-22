"""Live check that an expert claims nothing: in every swarm that is not stopped, no ledger task is claimed by an
expert."""

import sys
from collections.abc import Callable, Iterable
from typing import Any, Protocol

Tasks = Callable[[str], list[dict[str, Any]]]


class Fleet(Protocol):
    """What the check reads of the swarm store: the swarms and each one's state."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...


def main(store: Fleet | None = None, tasks: Tasks | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
