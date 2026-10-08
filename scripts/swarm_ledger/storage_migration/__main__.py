"""Usage: agentihooks ledger storage export SLUG [--out PATH] | import PATH [--slug SLUG] [--replace] | cutover"""

import argparse
import json
import sys

from . import cutover, export, load


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentihooks ledger storage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("export", help="print or write the complete stored document")
    out.add_argument("slug")
    out.add_argument("--out")
    into = sub.add_parser("import", help="store an exported document")
    into.add_argument("path")
    into.add_argument("--slug")
    into.add_argument("--replace", action="store_true")
    sub.add_parser("cutover", help="import every ledger file left in the ledger folder")
    args = parser.parse_args()
    try:
        if args.command == "export":
            state = export(args.slug, args.out)
            print(json.dumps({"exported": args.slug, "out": args.out}) if args.out else json.dumps(state))
        elif args.command == "import":
            print(json.dumps({"imported": load(args.path, args.slug, args.replace)}))
        else:
            print(json.dumps({"stored": cutover()}))
    except (KeyError, ValueError, OSError) as exc:
        sys.exit(f"agentihooks ledger storage: {exc}")


if __name__ == "__main__":
    main()
