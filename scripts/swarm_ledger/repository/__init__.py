import sys
from typing import Protocol, runtime_checkable

from scripts.swarm_ledger import HERE

sys.path.insert(0, str(HERE))

from .file import FileLedgerRepository
from .sqlite import SQLiteLedgerRepository as SQLiteLedgerRepository


@runtime_checkable
class LedgerRepository(Protocol):
    def get_document(self, slug: str, reconcile: bool = True) -> dict: ...

    def apply_ops(
        self, slug: str, changes: list | None = None, ops: list | None = None, gate=None
    ) -> tuple[dict, list]: ...

    def events_since(self, slug: str, revision: int) -> list: ...

    def list_summaries(self) -> list: ...

    def create(self, slug: str, content: dict, size: str = "small") -> bool: ...

    def delete(self, slug: str, now: int | None = None) -> None: ...

    def restore(self, slug: str, now: int | None = None) -> bool: ...


repository = FileLedgerRepository()
