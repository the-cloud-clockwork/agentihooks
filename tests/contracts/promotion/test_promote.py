import subprocess
from pathlib import Path


def test_promotion_api_contracts():
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("promote.cjs"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
