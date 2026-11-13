"""The CI lab command line: python -m scripts.ci_lab cutover --grader-root PATH, or scheduler|worker --self-test."""

import os
import sys
from collections.abc import Mapping, Sequence

from scripts.ci_lab import contract, delivery

SERVICES = {contract.SCHEDULER.name: contract.SCHEDULER, contract.WORKER.name: contract.WORKER}


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Dispatch a CI lab command and return its exit code."""
    if argv and argv[0] in SERVICES:
        return contract.run(SERVICES[argv[0]], argv[1:], environ)
    return delivery.main(argv, environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
