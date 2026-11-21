import sqlite3
from typing import Any


class LedgerUnreachable(Exception):
    pass


def store_errors() -> tuple[type[Exception], ...]:
    return (ConnectionError,)


def _finishing(store: Any, service: Any, proof_swarm: Any, lock: Any) -> None:
    pass


def _journal_pass(number: int, outcome: str) -> None:
    pass


def unguarded(store: Any) -> int:
    # ruleid: controller-pass-loop-ledger-storage-error
    _finishing(store, None, None, None)
    return 0


def guards_only_the_unreachable_ledger(store: Any, number: int) -> int:
    try:
        # ruleid: controller-pass-loop-ledger-storage-error
        _finishing(store, None, None, None)
    except LedgerUnreachable as exc:
        _journal_pass(number, f"ledger_unreachable {exc}")
        return 1
    except store_errors() as exc:
        _journal_pass(number, f"redis_grace {exc}")
        return 1
    return 0


def guards_the_socket_only(store: Any, number: int) -> int:
    try:
        # ruleid: controller-pass-loop-ledger-storage-error
        _finishing(store, None, None, None)
    except (LedgerUnreachable, OSError) as exc:
        _journal_pass(number, f"ledger_unreachable {exc}")
        return 1
    return 0


def guarded(store: Any, number: int) -> int:
    try:
        # ok: controller-pass-loop-ledger-storage-error
        _finishing(store, None, None, None)
    except (LedgerUnreachable, sqlite3.Error) as exc:
        _journal_pass(number, "ledger_unreachable" if isinstance(exc, LedgerUnreachable) else "ledger_storage")
        return 1
    except store_errors() as exc:
        _journal_pass(number, f"redis_grace {exc}")
        return 1
    return 0
