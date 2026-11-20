import sqlite3

WAL = "PRAGMA journal_mode=WAL"


def set_up(connection: sqlite3.Connection) -> None:
    # ruleid: ledger-wal-switch-without-lock-wait
    connection.execute("PRAGMA journal_mode=WAL")
    # ruleid: ledger-wal-switch-without-lock-wait
    connection.execute(WAL)
    # ruleid: ledger-wal-switch-without-lock-wait
    connection.execute("pragma journal_mode = wal")
    # ok: ledger-wal-switch-without-lock-wait
    connection.execute("PRAGMA journal_mode")
    # ok: ledger-wal-switch-without-lock-wait
    connection.execute("PRAGMA synchronous=FULL")


def wal(connection: sqlite3.Connection) -> None:
    # ruleid: ledger-wal-switch-without-lock-wait
    connection.execute(WAL)


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def set_up(self) -> None:
        # ruleid: ledger-wal-switch-without-lock-wait
        self.connection.execute(WAL)


class WalSwitch:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def turn(self) -> None:
        # ok: ledger-wal-switch-without-lock-wait
        self.connection.execute(WAL)
