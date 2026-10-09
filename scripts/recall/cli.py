import argparse
import json
import os
from collections.abc import Mapping

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
    return parser


def main(argv: list[str], environ: Mapping[str, str] = os.environ) -> int:
    args = _parser().parse_args(argv)
    folder = ledger_dir(environ)
    if args.ledger and not (folder / f"{args.ledger}.json").is_file():
        print(json.dumps({"error": f"no ledger file for {args.ledger}"}))
        return 1
    slugs = [args.ledger] if args.ledger else all_slugs(folder)
    store = SQLiteRecallStore(default_path(environ))
    result = reindex(store, folder, agentihooks_home(environ), slugs, args.include_binned)
    print(json.dumps(result))
    return 0
