import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CASE = "tests/inbox/test_channel_reconnect.py::test_running_channel_rechecks_pending_items_after_redis_returns"
SUBSCRIBER = "tests/inbox/test_channel_reconnect.py::test_channel_replaces_a_failed_subscriber_and_resets_its_backoff"

PROBE = """
import builtins, importlib.abc, json, os, signal, sys, time

import pytest

armed = []
real = signal.setitimer


class PlantedDelay(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "fakeredis":
            sys.meta_path.remove(self)
            time.sleep(float(os.environ["PLANTED_DELAY"]))


if os.environ.get("PLANTED_DELAY"):
    sys.meta_path.insert(0, PlantedDelay())

stalled = []
real_import = builtins.__import__


def stall(name, *args, **kwargs):
    if armed and not stalled:
        stalled.append(name)
        time.sleep(float(os.environ["IMPORT_STALL"]))
    return real_import(name, *args, **kwargs)


def arm(which, seconds):
    if seconds:
        server = sys.modules.get("mcp.server")
        armed.append(
            {
                "stdio": hasattr(server, "stdio"),
                "message": "mcp.shared.message" in sys.modules,
                "fakeredis": "fakeredis" in sys.modules,
            }
        )
    return real(which, seconds)


class Recorder:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        signal.setitimer = arm
        if os.environ.get("IMPORT_STALL"):
            builtins.__import__ = stall

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        if os.environ.get("CALL_STALL"):
            time.sleep(float(os.environ["CALL_STALL"]))


code = pytest.main([sys.argv[1], "-q", "-p", "no:cacheprovider"], plugins=[Recorder()])
print(json.dumps({"code": int(code), "armed": armed, **({"stalled": stalled} if os.environ.get("IMPORT_STALL") else {})}))
"""


def _probe(case=CASE, **planted):
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, "-c", PROBE, case],
        cwd=Path(__file__).parents[2],
        env={**env, **planted},
        capture_output=True,
        text=True,
        timeout=300,
    )
    return json.loads(result.stdout.splitlines()[-1]), result.stdout + result.stderr


def test_the_channel_reconnect_alarm_is_armed_only_after_the_sdk_is_loaded():
    report, output = _probe()
    assert report == {"code": 0, "armed": [{"stdio": True, "message": True, "fakeredis": True}]}, output


def test_a_slow_cold_fakeredis_import_is_not_charged_to_the_reconnect_alarm():
    report, output = _probe(PLANTED_DELAY="6")
    assert report == {"code": 0, "armed": [{"stdio": True, "message": True, "fakeredis": True}]}, output


@pytest.mark.parametrize("case", [CASE, SUBSCRIBER])
def test_an_import_that_stalls_inside_the_reconnect_alarm_window_is_not_charged_to_it(case):
    report, output = _probe(case, IMPORT_STALL="6")
    assert report["code"] == 0 and report["stalled"], output


def test_the_reconnect_alarm_still_fires_on_a_stall_outside_imports():
    report, output = _probe(CALL_STALL="6")
    assert report["code"] == 1 and "recovery loop exceeded its deadline" in output, output
