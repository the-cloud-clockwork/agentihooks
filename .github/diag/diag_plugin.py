import os
import sys
import time

T0 = float(os.environ["DIAG_T0"])
WHO = os.environ.get("PYTEST_XDIST_WORKER", "ctl")


def mark(name):
    sys.__stderr__.write(f"DIAG {WHO} {os.getpid()} {name} {time.time() - T0:.3f}\n")
    sys.__stderr__.flush()


mark("plugin-import")


def pytest_configure(config):
    mark("configure")


def pytest_sessionstart(session):
    mark("sessionstart")


def pytest_collection(session):
    mark("collection")


def pytest_collection_finish(session):
    mark("collection-finish")


def pytest_runtest_logstart(nodeid, location):
    if not getattr(pytest_runtest_logstart, "done", False):
        pytest_runtest_logstart.done = True
        mark("first-test")
