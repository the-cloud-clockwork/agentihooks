import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from . import tools
from .evaluate import GoldenError, evaluate, load_golden
from .reindex import all_slugs, ledger_dir, reindex
from .store import SQLiteRecallStore, agentihooks_home, default_path

SCOPE = "Ledger or swarm slug, or all; defaults to your own swarm"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks recall", description="Recall archive of ledgers and swarms")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("reindex", help="Backfill the recall archive from the stored ledgers")
    target = backfill.add_mutually_exclusive_group(required=True)
    target.add_argument("--ledger", metavar="SLUG", help="Index one ledger")
    target.add_argument("--all", action="store_true", help="Index every stored ledger")
    backfill.add_argument("--include-binned", action="store_true", help="Also index ledgers in the bin")
    grade = commands.add_parser("eval", help="Print the top five hit rate of a golden question file")
    grade.add_argument("golden", help="JSON list of question, expect and optional scope and kinds")
    find = commands.add_parser("search", help="Ranked hits with parent chains and snippets")
    find.add_argument("query")
    find.add_argument("--author", default="")
    find.add_argument("--seat", default="")
    find.add_argument("--limit", type=int, default=10)
    item = commands.add_parser("get", help="One item in full with its parent chain and comments")
    item.add_argument("ref", help="Item key such as LEDGER/tasks/ID, or a ref inside the scope")
    item.add_argument("--scope", default="", help=SCOPE)
    order = commands.add_parser("timeline", help="Items in time order")
    order.add_argument("--limit", type=int, default=200)
    for command in (find, order):
        command.add_argument("--scope", default="", help=SCOPE)
        command.add_argument("--kind", action="append", default=[], help="Record kind; repeat for several")
        command.add_argument("--since", default="", help="ISO time, epoch milliseconds or a span such as 1d")
        command.add_argument("--until", default="", help="ISO time, epoch milliseconds or a span such as 1d")
    commands.add_parser("sources", help="Indexed ledgers and swarms with counts, last indexed time and bin flag")
    return parser


def _eval(golden: str, environ: Mapping[str, str]) -> int:
    try:
        entries = load_golden(Path(golden))
    except GoldenError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(evaluate(SQLiteRecallStore(default_path(environ)), entries)))
    return 0


def _query(args: argparse.Namespace, environ: Mapping[str, str]) -> dict:
    if args.command == "get":
        return tools.get(args.ref, args.scope, environ)
    if args.command == "sources":
        return tools.sources(environ)
    period = f"{args.since}..{args.until}"
    chosen = tools.filters(
        args.scope, ",".join(args.kind), getattr(args, "author", ""), getattr(args, "seat", ""), period
    )
    if args.command == "search":
        return tools.search(args.query, chosen, args.limit, environ)
    return tools.timeline(chosen, args.limit, environ)


def main(argv: list[str], environ: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    if args.command == "eval":
        return _eval(args.golden, environ)
    if args.command != "reindex":
        result = _query(args, environ)
        print(json.dumps(result))
        return 0 if result["success"] else 1
    folder = ledger_dir(environ)
    if args.ledger and args.ledger not in all_slugs(folder):
        print(json.dumps({"error": f"no stored ledger for {args.ledger}"}))
        return 1
    slugs = [args.ledger] if args.ledger else all_slugs(folder)
    store = SQLiteRecallStore(default_path(environ))
    result = reindex(store, folder, agentihooks_home(environ), slugs, args.include_binned)
    print(json.dumps(result))
    return 0
