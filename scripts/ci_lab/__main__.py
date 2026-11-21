"""The CI lab command line: python -m scripts.ci_lab cutover --grader-root PATH, evidence --grade RUN,
shadow report --since REVISION, shadow report --gate GATE [--heads N] [--event merge_group], scheduler
[--interval S] [--passes N] to run the scheduler service, worker to run the worker service,
scheduler|worker --self-test, or compact to move the test batch manifests older lab Redis records carry to the object
store."""

import os
import sys
from collections.abc import Mapping, Sequence

from scripts.ci_lab import contract, delivery, evidence, shadow, shadow_report

SERVICES = {contract.SCHEDULER.name: contract.SCHEDULER, contract.WORKER.name: contract.WORKER}
SELF_TEST = "--self-test"


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Dispatch a CI lab command and return its exit code."""
    if argv[:1] == [contract.SCHEDULER.name] and SELF_TEST not in argv:
        from scripts.ci_lab import scheduler

        return scheduler.main(argv[1:], environ)
    if argv[:1] == [contract.WORKER.name] and SELF_TEST not in argv:
        from scripts.ci_lab import executors

        return executors.main(argv[1:], environ)
    if argv and argv[0] in SERVICES:
        return contract.run(SERVICES[argv[0]], argv[1:], environ)
    if argv and argv[0] == "grade":
        from scripts.ci_lab import plant_grade

        return plant_grade.main(argv[1:], environ)
    if argv and argv[0] == "evidence":
        return evidence.main(argv[1:])
    if argv and argv[0] == "compact":
        from scripts.ci_lab import compact

        return compact.main(argv[1:], environ)
    if argv and argv[0] == "shadow" and shadow_report.claims(argv[1:]):
        return shadow_report.main(argv[2:], environ)
    if argv and argv[0] == "shadow":
        return shadow.main(argv[1:], environ)
    return delivery.main(argv, environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
