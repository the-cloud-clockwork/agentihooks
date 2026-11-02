"""agentihooks knowledge: ask a ledger what it is and how it is evolving, or refresh its index."""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import service
from .chunks import Document
from .index import HashEmbedder, KnowledgeIndex


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--limit", type=int, default=5)
    refresh = commands.add_parser("refresh")
    for command in (ask, refresh):
        command.add_argument("--ledger", default="")
    return parser


def read_ledger(slug: str) -> Document:
    """The stored ledger document, read through the shared ledger repository."""
    from scripts.swarm_ledger import storage

    repository: Any = storage.default()
    return repository.get_document(slug)


def main(
    argv: list[str],
    environ: Mapping[str, str] = os.environ,
    reader: Callable[[str], Document] = read_ledger,
    clock: Callable[[], float] = time.time,
) -> int:
    """Run one knowledge command and print its JSON answer."""
    args = _parser().parse_args(argv)
    slug = args.ledger or environ.get("AGENTIHOOKS_SWARM", "")
    if not slug:
        print("knowledge: name a ledger with --ledger", file=sys.stderr)
        return 2
    index = KnowledgeIndex(service.index_path(slug, environ), HashEmbedder())
    now = int(clock() * 1000)
    if args.command == "refresh":
        answer = vars(service.refresh(index, reader(slug), now))
    else:
        answer = service.ask(index, lambda: reader(slug), args.question, now, args.limit)
    print(json.dumps({"ledger_slug": slug, **answer}, ensure_ascii=False, indent=1))
    return 0
