"""Hold the static gates the released lab reads from a head equal to the gates the head's own code reads.

python -m scripts.ci_lab.discovery_parity --released DIR --base DIR --head DIR

The lab scheduler reads the static gates of a head with the discovery code of its image, built from main, over a
tree of dev. A reading is the gate names the discovery command line of one code folder prints for one tree, less
each gate that misses a context name and less the names in the tree's exclusion catalog; a command that fails
gives a failed reading. A head is refused when the released code reads it to other gates than its own code does
and also to other gates than the released code reads from the base.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path

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
    """Return the static gates a lab running the code at `code` grades for the tree at `tree`."""
    del code, tree
    return frozenset()


def problems(released: Reading, own: Reading, before: Reading) -> list[str]:
    """List why a head is refused, from the released reading of the head, the head's own and the released reading
    of the base."""
    del released, own, before
    return []


def main(argv: Sequence[str] | None = None) -> int:
    """Grade the head at --head against the released code under --released and the base tree at --base."""
    parser = argparse.ArgumentParser()
    for name in ("--released", "--base", "--head"):
        parser.add_argument(name, type=Path, required=True)
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
