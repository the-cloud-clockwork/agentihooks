import argparse
import json

from . import core, import_directory


def main() -> None:
    parser = argparse.ArgumentParser(description="Import and verify authoritative ledgers in SQLite shadow storage")
    parser.add_argument("--directory", default=core.LEDGER_DIR)
    parser.add_argument("--database")
    args = parser.parse_args()
    print(json.dumps({"verified": import_directory(args.directory, args.database)}))


if __name__ == "__main__":
    main()
