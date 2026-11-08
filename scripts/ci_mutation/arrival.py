"""Names each mutation stats producer job whose uploaded part never reached a consumer's stats folder."""

import argparse
import json
import os
import sys
from pathlib import Path


def missing_producers(folder: Path, parts: list[int]) -> list[str]:
    """Return the job name of every stats producer whose collected marker is absent from folder."""
    return [f"mutation-stats ({part})" for part in parts if not (folder / f"collected-{part}").is_file()]


def main(argv: list[str] | None = None) -> int:
    """Report missing stats producers to the job output; with --required, fail naming them as a producer error."""
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_mutation.arrival")
    parser.add_argument("folder", type=Path)
    parser.add_argument("parts", type=json.loads)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args(argv)
    missing = missing_producers(args.folder, args.parts)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"missing={', '.join(missing)}\n")
    if not missing:
        print(f"Every mutation stats producer's part arrived: {len(args.parts)} of {len(args.parts)}")
        return 0
    message = f"mutation stats producer error: no part from {', '.join(missing)} reached {args.folder}"
    if args.required:
        print(f"::error::{message}; the shard grades nothing without it")
        return 1
    print(f"::warning::{message}; downloading the shared mutation stats again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
