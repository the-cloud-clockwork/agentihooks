from itertools import islice

from scripts.ci_lab import shadow_report


def walked(fetch: object, repository: str, gates: tuple, sources: object, heads: int) -> dict:
    return {
        gate: shadow_report.tally(
            # ruleid: doctor-reader-walks-the-acceptance-report
            shadow_report.collect(fetch, repository, shadow_report.PULL_REQUEST, gate, sources, heads)
        ).mismatches
        for gate in gates
    }


def walked_once(fetch: object, repository: str, sources: object) -> list:
    # ruleid: doctor-reader-walks-the-acceptance-report
    return shadow_report.collect(fetch, repository, "pull_request", "lint", sources, 10)


def bounded(fetch: object, repository: str, gates: tuple, sources: object, heads: int) -> dict:
    # ok: doctor-reader-walks-the-acceptance-report
    newest = islice(shadow_report.heads_of(fetch, repository, shadow_report.PULL_REQUEST), heads)
    # ok: doctor-reader-walks-the-acceptance-report
    read = [(sha, shadow_report.check_runs(fetch, repository, sha)) for sha in newest]
    return {
        # ok: doctor-reader-walks-the-acceptance-report
        gate: sum(shadow_report.head(sha, runs, gate, sources, listed=True) is not None for sha, runs in read)
        for gate in gates
    }


def collect(found_by: dict) -> list:
    # ok: doctor-reader-walks-the-acceptance-report
    return [name for name in found_by]
