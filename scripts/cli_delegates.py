import importlib
from collections.abc import Callable

DELEGATED_CLIS = {
    "msg": "scripts.inbox.cli",
    "recall": "scripts.recall.cli",
    "trace": "scripts.trace_cli",
    "deps": "scripts.deps_preflight",
    "quota": "scripts.agents_quota",
    "plan": "scripts.swarm_ledger.plan_read",
}


def delegated_cli(argv: list[str]) -> Callable[[list[str]], int] | None:
    for name in argv[:1]:
        if name in DELEGATED_CLIS:
            return importlib.import_module(DELEGATED_CLIS[name]).main
