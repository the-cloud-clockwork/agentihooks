import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def inventory():
    folder = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    return {
        "folder": str(folder),
        "target": str(folder.resolve()),
        "symlink": folder.is_symlink(),
        "exists": folder.exists(),
        "executables": sorted(str(path.relative_to(folder)) for path in folder.glob("**/chrome-headless-shell")),
    }


def pytest_sessionstart(session):
    print("[BROWSER-LIFETIME] session " + json.dumps(inventory()), flush=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    before = inventory()
    yield
    after = inventory()
    if before != after:
        print("[BROWSER-LIFETIME] " + item.nodeid + " " + json.dumps({"before": before, "after": after}), flush=True)
        assert after["executables"], item.nodeid


def test_source_browser_lifetime():
    print("[BROWSER-LIFETIME] before suite " + json.dumps(inventory()), flush=True)
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
            "-p",
            "tests.browser_cache_lifetime_proof",
        ],
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
    )
    print(result.stdout, flush=True)
    print(result.stderr, flush=True)
    print("[BROWSER-LIFETIME] after suite " + json.dumps(inventory()), flush=True)
    assert result.returncode == 0
