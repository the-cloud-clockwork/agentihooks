"""agentihooks swarm <slug> tasks: every task row and freeze record of a swarm as one JSON document."""

import argparse
import json
from typing import Any

from scripts.swarm import ledger_client


def command(store: Any, args: argparse.Namespace) -> None:
    """Print the swarm's task rows and freeze and focus records, keyed tasks and freezes."""
    client = ledger_client.LedgerClient()
    print(json.dumps({"tasks": client.tasks(args.slug), "freezes": client.freezes(args.slug)}))
