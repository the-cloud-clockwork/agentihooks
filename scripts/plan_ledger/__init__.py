"""Plan ledger: a live plan page an operator and a crew of agents share. Standard library only."""

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = {"new": "new_ledger", "serve": "ledger_server", "watch": "watch_ledger", "chat": "chat_ledger"}


def run(argv):
    sys.path.insert(0, str(HERE))
    tool = TOOLS.get(argv[0]) if argv else None
    module = importlib.import_module(tool or "ledger")
    sys.argv = ["agentihooks ledger " + argv[0], *argv[1:]] if tool else ["agentihooks ledger", *argv]
    return module.main()
