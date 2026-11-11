from typing import Any


class Launch:
    def _master_selection(self, *args: Any) -> Any:
        return args

    def _select(self, rows: list[Any], front: bool, accounts: frozenset[str] = frozenset()) -> Any:
        # ruleid: master-retry-drops-refused-accounts
        return self._master_selection(rows, front)


class Fixed:
    def _master_selection(self, *args: Any) -> Any:
        return args

    def _select(self, rows: list[Any], front: bool, accounts: frozenset[str] = frozenset()) -> Any:
        # ok: master-retry-drops-refused-accounts
        return self._master_selection(rows, front, accounts)
