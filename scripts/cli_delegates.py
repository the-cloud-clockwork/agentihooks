import importlib
from collections.abc import Callable

DELEGATED_CLIS = {
    "hive": "scripts.hive.cli",
    "msg": "scripts.inbox.cli",
    "recall": "scripts.recall.cli",
    "trace": "scripts.trace_cli",
}


def delegated_cli(argv: list[str]) -> Callable[[list[str]], int] | None:
    for name in argv[:1]:
        if name in DELEGATED_CLIS:
            return importlib.import_module(DELEGATED_CLIS[name]).main
