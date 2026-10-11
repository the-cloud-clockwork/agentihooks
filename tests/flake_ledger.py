"""Writes one JSON line per test that needed a retry to the file named by CI_FLAKES."""

import json
import os
from pathlib import Path


def _error(report) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash else report.longreprtext
    return (text.strip().splitlines() or [""])[0][:300]


class FlakeLedger:
    def __init__(self, path: Path):
        self.path = path
        self.errors: dict[str, list[str]] = {}

    def pytest_runtest_logreport(self, report):
        if report.outcome == "rerun":
            self.errors.setdefault(report.nodeid, []).append(_error(report))
            return
        if report.when != "call" and not report.failed:
            return
        errors = self.errors.pop(report.nodeid, None)
        if errors is None:
            return
        if report.failed:
            errors.append(_error(report))
        record = {
            "test": report.nodeid,
            "run": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "attempts": len(errors) + report.passed,
            "outcome": "flaky" if report.passed else "failed",
            "errors": errors,
        }
        with self.path.open("a") as ledger:
            ledger.write(json.dumps(record) + "\n")


def pytest_configure(config):
    path = os.environ.get("CI_FLAKES")
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(FlakeLedger(Path(path)), "flake-ledger")
