"""The CI lab command line: python -m scripts.ci_lab cutover --grader-root PATH."""

import os
import sys
from collections.abc import Mapping, Sequence

from scripts.ci_lab import delivery


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Dispatch a CI lab command and return its exit code."""
    return delivery.main(argv, environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
