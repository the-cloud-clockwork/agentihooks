import re
import subprocess
from pathlib import Path

import pytest


@pytest.mark.feature("promotion-red-graded-job-fails-at-once")
def test_promotion_api_contracts() -> None:
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("promote.cjs"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.feature("promotion-current-job-not-completed")
def test_noncompleted_triggering_promotion_job_merges() -> None:
    result = subprocess.run(
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern=^a noncompleted triggering promote job merges",
            str(Path(__file__).with_name("promote.cjs")),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^# pass 4$", result.stdout, re.MULTILINE), result.stdout


@pytest.mark.feature("promotion-changing-grade-poll-diagnostics")
def test_changed_promotion_grade_polls_are_logged() -> None:
    result = subprocess.run(
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern=^logs ",
            str(Path(__file__).with_name("promote.cjs")),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^# pass 4$", result.stdout, re.MULTILINE), result.stdout


@pytest.mark.feature("promotion-unstable-snapshot-ignores-dev-commit")
def test_unstable_snapshot_merges_without_reading_the_dev_commit() -> None:
    result = subprocess.run(
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern=^an unstable snapshot merges ",
            str(Path(__file__).with_name("promote.cjs")),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^# pass 4$", result.stdout, re.MULTILINE), result.stdout
