"""Write one fsynced JSON line per finished test to a file per worker in the folder CI_RESULTS names.

Loaded with `-p tests.pass_record`. The controller takes CI_RESULTS out of the environment and hands it to its
workers, so pytest runs that tests start never record into it. pytest-rerunfailures wraps every attempt in its own
logstart and logfinish; only an attempt without a rerun report is final, and a final pass after a retry is flaky.
"""

import json
import os
from pathlib import Path

import pytest

SOURCE = pytest.StashKey[str]()


def _outcome(outcomes: list[str], retried: bool) -> str:
    if "failed" in outcomes:
        return "failed"
    if "skipped" in outcomes:
        return "skipped"
    return "flaky" if retried else "passed"


class PassRecord:
    def __init__(self, path: Path, config):
        self.path = path
        self.config = config
        self.attempts = {}
        self.retried = set()

    def pytest_runtest_logreport(self, report):
        self.attempts.setdefault(report.nodeid, []).append(report.outcome)
        if getattr(report, "rerun", 0):
            self.retried.add(report.nodeid)

    def flush(self, nodeid: str) -> None:
        # The teardown context stays open until the next switch, so its arcs would die with a killed worker.
        controller = getattr(self.config.pluginmanager.get_plugin("_cov"), "cov_controller", None)
        if controller and controller.started and self.config.pluginmanager.has_plugin("_cov_contexts"):
            controller.cov.switch_context(f"{nodeid}|record")

    def pytest_runtest_logfinish(self, nodeid):
        outcomes = self.attempts.pop(nodeid, [])
        if "rerun" in outcomes:
            self.retried.add(nodeid)
            return
        if not outcomes:
            return
        outcome = _outcome(outcomes, nodeid in self.retried)
        self.retried.discard(nodeid)
        self.flush(nodeid)
        with self.path.open("a") as results:
            results.write(json.dumps({"test": nodeid, "outcome": outcome, "pid": os.getpid()}) + "\n")
            results.flush()
            os.fsync(results.fileno())


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        folder = config.workerinput.get("ci_results", "")
    else:
        folder = os.environ.pop("CI_RESULTS", "")
    config.stash[SOURCE] = folder
    controller = not hasattr(config, "workerinput") and config.getoption("dist", "no") != "no"
    if not folder or controller:
        return
    path = Path(os.path.abspath(folder)) / f"{os.getpid()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    config.pluginmanager.register(PassRecord(path, config), "pass-record")


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput["ci_results"] = node.config.stash[SOURCE]
