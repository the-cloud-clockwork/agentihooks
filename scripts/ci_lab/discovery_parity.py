"""Hold the static gates the released lab reads from a head equal to the gates the head's own code reads.

python -m scripts.ci_lab.discovery_parity --released DIR --base DIR --head DIR

The lab scheduler reads the static gates of a head with the discovery code of its image, built from main, over a
tree of dev. A reading is the gate names the discovery command line of one code folder prints for one tree, less
each gate that misses a context name and less the names in the tree's exclusion catalog; a command that fails
gives a failed reading. A head is refused when the released code reads it to other gates than its own code does
and also to other gates than the released code reads from the base.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.ci_lab import static_exclusions

COMMAND = ("-m", "scripts.ci_lab.static_gates", "--root")
DIFFERS = (
    "the released lab reads this head to other static gates than the head's own code does, and to other gates "
    "than it reads from the base"
)
BROKEN = "the released lab cannot read the static gates of this head, and it reads those of the base"
UNREADABLE = "the head's own code cannot read the static gates of the head"
LAGS = "the released lab reads this head to other static gates than the head's own code does, as it reads the base"
STUCK = "the released lab cannot read the static gates of this head, as it cannot read those of the base"
REMEDY = "land the change to how gates are read first, and the tree that needs it once a release holds that change"

Reading = frozenset[str] | None


def graded(code: Path, tree: Path) -> Reading:
    """Return the static gates a lab running the code at `code` grades for the tree at `tree`: the names its
    discovery command line prints, run in its own process from that folder with that folder first on the module
    path, less each gate that misses a context name and less the names in the tree's exclusion catalog. A command
    that fails gives no reading, and its error goes to standard error."""
    folder = code.resolve()
    command = [sys.executable, *COMMAND, str(tree.resolve())]
    environ = {**os.environ, "PYTHONPATH": str(folder)}
    done = subprocess.run(command, cwd=folder, env=environ, capture_output=True, text=True, check=False)
    if done.returncode:
        sys.stderr.write(done.stderr)
        return None
    gates = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    catalog = tree / static_exclusions.PATH
    excluded = static_exclusions.read(catalog.read_text()) if catalog.is_file() else {}
    return frozenset(gate["name"] for gate in gates if not gate["missing"] and gate["name"] not in excluded)


def _sides(released: frozenset[str], own: frozenset[str]) -> list[str]:
    """Name the gates only the released code grades and the gates only the head's code grades, each as none when
    it has none."""
    return [
        f"only the released code grades: {', '.join(sorted(released - own)) or 'none'}",
        f"only the head's code grades: {', '.join(sorted(own - released)) or 'none'}",
    ]


def problems(released: Reading, own: Reading, before: Reading) -> list[str]:
    """List why a head is refused, from the released reading of the head, the head's own and the released reading
    of the base: its own code cannot read it, or the released reading is neither the head's own nor that of the
    base."""
    if own is None:
        return [UNREADABLE]
    if released in (own, before):
        return []
    if released is None:
        return [BROKEN, REMEDY]
    return [DIFFERS, *_sides(released, own), REMEDY]


def notes(released: Reading, own: frozenset[str]) -> list[str]:
    """List what a head that is not refused is told when the released reading is not its own: the release reads
    neither tree, or it reads this head as it reads the base."""
    if released is None:
        return [STUCK]
    return [] if released == own else [LAGS, *_sides(released, own)]


def main(argv: Sequence[str] | None = None) -> int:
    """Grade the head at --head against the released code under --released and the base tree at --base: print each
    refusal as an error and exit 1, else print the notes, or the count of gates when the two codes agree."""
    parser = argparse.ArgumentParser()
    for name in ("--released", "--base", "--head"):
        parser.add_argument(name, type=Path, required=True)
    args = parser.parse_args(argv)
    released, own = graded(args.released, args.head), graded(args.head, args.head)
    found = problems(released, own, graded(args.released, args.base))
    if found or own is None:
        for line in found:
            print(f"::error::{line}")
        return 1
    told = notes(released, own)
    for line in told:
        print(f"::notice::{line}")
    if not told:
        print(f"Discovery parity: the released code and the head read {len(own)} gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
