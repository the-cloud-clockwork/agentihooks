from collections.abc import Mapping

PULL_REQUIRED = ("lint", "semgrep")


def looks_each_job_up_by_its_bare_name(gates: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    # ruleid: lab-pull-frame-drops-reusable-job-units
    return {job: gates.get(job, ()) for job in PULL_REQUIRED}


def folds_the_called_jobs_under_each_job(gates: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    # ok: lab-pull-frame-drops-reusable-job-units
    return {
        job: tuple(unit for key, units in gates.items() if key == job or key.startswith(f"{job}/") for unit in units)
        for job in PULL_REQUIRED
    }
