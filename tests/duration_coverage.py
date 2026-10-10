import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class IncompleteDurations(ValueError):
    pass


def _collect(root: Path, paths: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *paths,
            "--collect-only",
            "-q",
            "--assert=plain",
            "-p",
            "no:cacheprovider",
            "-n",
            "0",
            "-o",
            "addopts=",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )


def collected_tests(root: Path) -> list[str]:
    files = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    count = min(len(files), os.cpu_count() or 1) or 1
    chunks = [set(files[i::count]) for i in range(count)]
    with ThreadPoolExecutor(count) as pool:
        results = list(
            pool.map(
                lambda chunk: _collect(root, ["tests/", *(f"--ignore={path}" for path in files if path not in chunk)]),
                chunks,
            )
        )
    failed = [result for result in results if result.returncode not in (0, 5)]
    if failed:
        raise RuntimeError("".join(result.stdout + result.stderr for result in failed))
    order = {path: i for i, path in enumerate(files)}
    collected = dict.fromkeys(
        line for result in results for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line
    )
    if not collected:
        raise RuntimeError("No tests collected for durations coverage")
    return [
        re.sub(r"@[^\[\]]*$", "", nodeid)
        for nodeid in sorted(collected, key=lambda nodeid: order.get(nodeid.split("::", 1)[0], -1))
    ]


def validate_coverage(durations: dict[str, float], collected: list[str]) -> None:
    missing = sum(nodeid not in durations for nodeid in collected)
    if missing * 10 > len(collected):
        raise IncompleteDurations(f"{missing} of {len(collected)} tests have no stored duration")
