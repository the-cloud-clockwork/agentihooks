PULL_REQUIRED = ("lint", "semgrep")
QUEUE_REQUIRED = ("select", "split")
UNIT_JOBS = ("unit", "unit-lost", "unit-rerun")
SCOPED = {"full": ("split", "unit", "shard-check"), "selected": ("unit-selected",)}


def names_the_unit_job_beside_the_pull_request_jobs(plant: bool) -> tuple[str, ...]:
    # ruleid: lab-pull-frame-names-unit-job-outside-its-scope
    return (*PULL_REQUIRED, UNIT_JOBS[0]) if plant else PULL_REQUIRED


def adds_the_unit_job_to_the_pull_request_jobs(plant: bool) -> tuple[str, ...]:
    # ruleid: lab-pull-frame-names-unit-job-outside-its-scope
    return PULL_REQUIRED + (UNIT_JOBS[0],) if plant else PULL_REQUIRED


def takes_the_test_jobs_from_the_scope(plant: bool) -> tuple[str, ...]:
    scope = "full" if plant else "none"
    # ok: lab-pull-frame-names-unit-job-outside-its-scope
    return (*PULL_REQUIRED, *SCOPED.get(scope, ()))


def names_the_unit_job_beside_the_queue_jobs() -> tuple[str, ...]:
    # ok: lab-pull-frame-names-unit-job-outside-its-scope
    return (*QUEUE_REQUIRED, UNIT_JOBS[0])
