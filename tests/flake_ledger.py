"""Writes one JSON line per test that needed a retry to the file named by CI_FLAKES.

pytest-rerunfailures wraps every attempt in its own logstart and logfinish, so a test is final
at the first logfinish whose attempt logged no rerun report. A worker crash ends its attempt
without a logfinish, and a scope scheduler runs the crashed test again after every crash, so a
crashed test is written once at session end with every attempt it took.
"""

import json
import os
from pathlib import Path

import pytest
from xdist.scheduler import LoadScopeScheduling


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
        self.crashes = {}
        self.crashing = set()
        self.crash_failed = {}

    @pytest.hookimpl(trylast=True)
    def pytest_handlecrashitem(self, crashitem, report, sched):
        # A scope scheduler puts the crashed test back in its queue but refuses
        # pytest-rerunfailures' mark_test_pending, so the report stays failed.
        self.crashes[crashitem] = self.crashes.get(crashitem, 0) + 1
        self.crashing.add(crashitem)
        retry = self.crashes[crashitem] == 1 and sched.config.getoption("reruns", 0)
        if report.failed and retry and isinstance(sched, LoadScopeScheduling):
            report.outcome = "rerun"

    def pytest_runtest_logreport(self, report):
        if report.outcome == "rerun":
            self.retrying.add(report.nodeid)
        elif report.failed:
            self.failed.add(report.nodeid)
        else:
            return
        self.errors.setdefault(report.nodeid, []).append(_error(report))
        if report.nodeid in self.crashing:
            self.crashing.discard(report.nodeid)
            self.pytest_runtest_logfinish(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid):
        self.attempts[nodeid] = self.attempts.get(nodeid, 0) + 1
        if nodeid in self.retrying:
            self.retrying.discard(nodeid)
            self.failed.discard(nodeid)
            return
        failed = nodeid in self.failed
        self.failed.discard(nodeid)
        if nodeid in self.crashes:
            self.crash_failed[nodeid] = failed or self.crash_failed.get(nodeid, False)
            return
        attempts = self.attempts.pop(nodeid)
        errors = self.errors.pop(nodeid, [])
        if attempts > 1:
            self._write(nodeid, attempts, failed, errors)

    def pytest_sessionfinish(self):
        for nodeid, crashes in self.crashes.items():
            failed = self.crash_failed.get(nodeid, True)
            self._write(nodeid, self.attempts[nodeid], failed, self.errors[nodeid], crashes=crashes)

    def _write(self, nodeid, attempts, failed, errors, **extra):
        record = {
            "test": nodeid,
            "run": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "attempts": attempts,
            "outcome": "failed" if failed else "flaky",
            "errors": errors,
            **extra,
        }
        with self.path.open("a") as ledger:
            ledger.write(json.dumps(record) + "\n")


def pytest_configure(config):
    path = os.environ.get("CI_FLAKES")
    if path and not hasattr(config, "workerinput"):
        config.pluginmanager.register(FlakeLedger(Path(path)), "flake-ledger")
