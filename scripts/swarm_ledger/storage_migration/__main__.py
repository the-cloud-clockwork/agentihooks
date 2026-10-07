import argparse
import json
from pathlib import Path

from . import core, import_directory


def main() -> None:
    parser = argparse.ArgumentParser(description="Import and verify authoritative ledgers in SQLite shadow storage")
    parser.add_argument("--directory", type=Path, default=core.LEDGER_DIR)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    print(json.dumps({"verified": import_directory(args.directory, args.database)}))


if __name__ == "__main__":
    main()
