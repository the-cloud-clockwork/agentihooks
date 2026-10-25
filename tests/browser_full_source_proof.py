import subprocess
import sys
from pathlib import Path


def test_full_source_group_keeps_the_baked_browser():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-n",
            "4",
            "--splits",
            "4",
            "--group",
            "1",
            "--dist",
            "loadgroup",
            "--tb=short",
            "-ra",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=820,
    )
    print(result.stdout)
    print(result.stderr)
    assert result.returncode == 0
    assert "Executable doesn't exist" not in result.stdout + result.stderr
