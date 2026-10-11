"""Writes one JSON line per test that needed a retry to the file named by CI_FLAKES.

pytest-rerunfailures wraps every attempt in its own logstart and logfinish, so a test is final
at the first logfinish whose attempt logged no rerun report.
"""

import json
import os
from pathlib import Path


def _error(report):
    crash = getattr(report.longrepr, "reprcrash", None)
    text = crash.message if crash else report.longreprtext
    return (text.strip().splitlines() or [""])[0][:300]


class FlakeLedger:
    def __init__(self, path: Path):
        self.path = path
        self.attempts = {}
        self.errors = {}
        self.retrying = set()
        self.failed = set()

    def pytest_runtest_logreport(self, report):
        if report.outcome == "rerun":
            self.retrying.add(report.nodeid)
        elif report.failed:
            self.failed.add(report.nodeid)
        else:
            return
        self.errors.setdefault(report.nodeid, []).append(_error(report))

    def pytest_runtest_logfinish(self, nodeid):
        self.attempts[nodeid] = self.attempts.get(nodeid, 0) + 1
        if nodeid in self.retrying:
            self.retrying.discard(nodeid)
            self.failed.discard(nodeid)
            return
        attempts = self.attempts.pop(nodeid)
        errors = self.errors.pop(nodeid, [])
        failed = nodeid in self.failed
        self.failed.discard(nodeid)
        if attempts == 1:
            return
        record = {
            "test": nodeid,
            "run": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "attempts": attempts,
            "outcome": "failed" if failed else "flaky",
            "errors": errors,
        }
        with self.path.open("a") as ledger:
            ledger.write(json.dumps(record) + "\n")


def pytest_configure(config):
    path = os.environ.get("CI_FLAKES")
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(FlakeLedger(Path(path)), "flake-ledger")
