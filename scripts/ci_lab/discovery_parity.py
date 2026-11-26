"""Hold the static gates the released lab reads from a head equal to the gates the head's own code reads.

python -m scripts.ci_lab.discovery_parity --released DIR --base DIR --head DIR

The lab scheduler reads static gates with the discovery code of its image, built from main, over a tree of dev: the
base tree for a pull request or merge group head, the head tree for a promotion or backfill head. A head that lands
on dev becomes that tree, the base of every later pull request and the tree of its own promotion grade, so the head
tree stands here for what the lab grades from then on. A reading is the gate names the discovery command line of one
code folder prints for one tree, less each gate that misses a context name and less the names the catalog reader of
that same code finds in the tree's exclusion catalog; a command that fails gives a failed reading. A head is refused
when the released code reads it to other gates than its own code does and also to other gates than the released code
reads from the base.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

COMMAND = ("-m", "scripts.ci_lab.static_gates", "--root")
CATALOG = (
    "-c",
    "import json, sys; from pathlib import Path; from scripts.ci_lab import static_exclusions as catalog; "
    "path = Path(sys.argv[1]) / catalog.PATH; "
    "print(json.dumps(sorted(catalog.read(path.read_text())) if path.is_file() else []))",
)
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


def _printed(folder: Path, arguments: Sequence[str]) -> str | None:
    """Return what a Python command of the code at `folder` prints, run in its own process from that folder with
    that folder first on the module path, or nothing when it fails, its error going to standard error."""
    environ = {**os.environ, "PYTHONPATH": str(folder)}
    command = [sys.executable, *arguments]
    done = subprocess.run(command, cwd=folder, env=environ, capture_output=True, text=True, check=False)
    if done.returncode:
        sys.stderr.write(done.stderr)
        return None
    return done.stdout


def graded(code: Path, tree: Path) -> Reading:
    """Return the static gates a lab running the code at `code` grades for the tree at `tree`: the names its
    discovery command line prints, less each gate that misses a context name and less the names its own catalog
    reader finds in the tree's exclusion catalog. A command or a reader that fails gives no reading."""
    folder, root = code.resolve(), str(tree.resolve())
    gates = _printed(folder, [*COMMAND, root])
    excluded = None if gates is None else _printed(folder, [*CATALOG, root])
    if gates is None or excluded is None:
        return None
    found = (json.loads(line) for line in gates.splitlines() if line.strip())
    return frozenset(gate["name"] for gate in found if not gate["missing"]) - frozenset(json.loads(excluded))


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
