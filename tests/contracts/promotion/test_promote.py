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
