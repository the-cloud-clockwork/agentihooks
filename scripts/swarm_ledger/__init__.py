"""Swarm ledger: a live plan page an operator and a crew of agents share. Standard library only."""

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = {
    "new": ("new_ledger", []),
    "storage": ("scripts.swarm_ledger.storage_migration.__main__", []),
    "serve": ("ledger_server", []),
    "watch": ("watch_ledger", []),
    "chat": ("chat_ledger", []),
    "decline": ("ledger_decline", []),
}


def run(argv):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    known = bool(argv) and argv[0] in TOOLS
    module, prefix = TOOLS[argv[0]] if known else ("ledger", [])
    saved = sys.argv
    sys.argv = [
        f"agentihooks ledger {argv[0]}" if known else "agentihooks ledger",
        *prefix,
        *(argv[1:] if known else argv),
    ]
    try:
        return importlib.import_module(module).main()
    finally:
        sys.argv = saved
