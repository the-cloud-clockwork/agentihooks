import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path

from .evaluate import GoldenError, evaluate, load_golden
from .reindex import all_slugs, ledger_dir, reindex
from .store import SQLiteRecallStore, agentihooks_home, default_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks recall", description="Recall archive of ledgers and swarms")
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("reindex", help="Backfill the recall archive from ledger files")
    target = backfill.add_mutually_exclusive_group(required=True)
    target.add_argument("--ledger", metavar="SLUG", help="Index one ledger")
    target.add_argument("--all", action="store_true", help="Index every ledger file")
    backfill.add_argument("--include-binned", action="store_true", help="Also index ledgers in the bin")
    grade = commands.add_parser("eval", help="Print the top five hit rate of a golden question file")
    grade.add_argument("golden", help="JSON list of question, expect and optional scope and kinds")
    return parser


def _eval(golden: str, environ: Mapping[str, str]) -> int:
    try:
        entries = load_golden(Path(golden))
    except GoldenError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(evaluate(SQLiteRecallStore(default_path(environ)), entries)))
    return 0


def main(argv: list[str], environ: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    if args.command == "eval":
        return _eval(args.golden, environ)
    folder = ledger_dir(environ)
    if args.ledger and not (folder / f"{args.ledger}.json").is_file():
        print(json.dumps({"error": f"no ledger file for {args.ledger}"}))
        return 1
    slugs = [args.ledger] if args.ledger else all_slugs(folder)
    store = SQLiteRecallStore(default_path(environ))
    result = reindex(store, folder, agentihooks_home(environ), slugs, args.include_binned)
    print(json.dumps(result))
    return 0
