FAILED_JOB = ("failure", "timed_out", "startup_failure")
SUMMARY = "Queue gate summary"


def failed_on_any_job(jobs: list[dict[str, str]]) -> bool:
    # ruleid: queue-exit-counts-gate-summary
    return any(job["conclusion"] in FAILED_JOB for job in jobs)


def failed_on_a_job_beside_the_summary(jobs: list[dict[str, str]]) -> bool:
    # ok: queue-exit-counts-gate-summary
    return any(job["conclusion"] in FAILED_JOB and job["name"] != SUMMARY for job in jobs)
