import re
import subprocess
from pathlib import Path

import pytest


@pytest.mark.feature("promotion-current-job-not-completed")
@pytest.mark.feature("promotion-changing-grade-poll-diagnostics")
@pytest.mark.feature("promotion-red-graded-job-fails-at-once")
def test_promotion_api_contracts() -> None:
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("promote.cjs"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
