import json
import os
import subprocess
import sys
from pathlib import Path

CASE = "tests/inbox/test_channel_reconnect.py::test_running_channel_rechecks_pending_items_after_redis_returns"

PROBE = """
import importlib.abc, json, os, signal, sys, time

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


code = pytest.main([sys.argv[1], "-q", "-p", "no:cacheprovider"], plugins=[Recorder()])
print(json.dumps({"code": int(code), "armed": armed}))
"""


def _probe(**planted):
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, "-c", PROBE, CASE],
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
