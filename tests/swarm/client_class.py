"""A stand in class for scripts.swarm.ledger_client.LedgerClient that hands every caller one prepared fake."""

from typing import Any


def client_class(ledger: object) -> type[Any]:
    """Return a class whose every construction returns ``ledger``.

    A lambda or plain function in place of LedgerClient breaks each module first imported under the patch, because
    only a class supports the ``LedgerClient | None`` annotation such a module evaluates at import.
    """

    class Client:
        def __new__(cls, *args: object, **kwargs: object) -> Any:
            return ledger

    return Client
