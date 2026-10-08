import os
import re
import subprocess
import sys
from pathlib import Path


class IncompleteDurations(ValueError):
    pass


def collected_tests(root: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-n", "0", "-o", "addopts="],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    collected = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    if not collected:
        raise RuntimeError("No tests collected for durations coverage")
    return [re.sub(r"@[^\[\]]*$", "", nodeid) for nodeid in collected]


def validate_coverage(durations: dict[str, float], collected: list[str]) -> None:
    missing = sum(nodeid not in durations for nodeid in collected)
    if missing * 10 > len(collected):
        raise IncompleteDurations(f"{missing} of {len(collected)} tests have no stored duration")
