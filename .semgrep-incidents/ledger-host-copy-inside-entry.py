from typing import Any


class Immediate:
    def _entry(self, connection: Any, slug: str, entry: Any, whole: bool) -> Any:
        # ruleid: ledger-host-copy-inside-entry
        self.copies.keep(slug, entry, whole)
        return entry


class Deferred:
    def _entry(self, connection: Any, slug: str, entry: Any, whole: bool) -> Any:
        # ok: ledger-host-copy-inside-entry
        self._keep(connection, slug, entry, whole)
        return entry
