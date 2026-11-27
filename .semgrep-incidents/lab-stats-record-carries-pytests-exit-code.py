from typing import Any

import pytest

from scripts.ci_lab import run_mutation
from scripts.ci_lab.run_mutation import collected_status


def recorded_with_the_exit_code_as_an_integer(chosen: list[str]) -> list[dict[str, Any]]:
    status = pytest.main(chosen)
    # ruleid: lab-stats-record-carries-pytests-exit-code
    return [{"status": int(status), "tests": {}, "durations": {}}]


def recorded_with_the_exit_code_as_it_came(chosen: list[str]) -> list[dict[str, Any]]:
    status = pytest.main(chosen)
    # ruleid: lab-stats-record-carries-pytests-exit-code
    return [{"tests": {}, "status": status}]


def recorded_with_the_session_inline(chosen: list[str]) -> dict[str, Any]:
    # ruleid: lab-stats-record-carries-pytests-exit-code
    return {"status": int(pytest.main(chosen)), "tests": {}}


def recorded_with_the_bare_session_inline(chosen: list[str]) -> dict[str, Any]:
    # ruleid: lab-stats-record-carries-pytests-exit-code
    return {"status": pytest.main(chosen)}


def recorded_with_no_tests_collected_read_as_empty(chosen: list[str]) -> list[dict[str, Any]]:
    status = pytest.main(chosen)
    # ok: lab-stats-record-carries-pytests-exit-code
    return [{"status": run_mutation.collected_status(int(status)), "tests": {}, "durations": {}}]


def recorded_by_name_with_no_tests_collected_read_as_empty(chosen: list[str]) -> dict[str, Any]:
    status = pytest.main(chosen)
    # ok: lab-stats-record-carries-pytests-exit-code
    return {"status": collected_status(int(status)), "tests": {}}


def refused_on_a_failed_session(chosen: list[str]) -> dict[str, Any]:
    status = pytest.main(chosen)
    if status:
        raise SystemExit(int(status))
    # ok: lab-stats-record-carries-pytests-exit-code
    return {"status": 0, "rows": []}
