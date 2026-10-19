import json
import os
import subprocess
import sys
from pathlib import Path

CASE = "tests/inbox/test_channel_reconnect.py::test_running_channel_rechecks_pending_items_after_redis_returns"

PROBE = """
import json, signal, sys

import pytest

armed = []
real = signal.setitimer


def arm(which, seconds):
    if seconds:
        server = sys.modules.get("mcp.server")
        armed.append({"stdio": hasattr(server, "stdio"), "message": "mcp.shared.message" in sys.modules})
    return real(which, seconds)


class Recorder:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        signal.setitimer = arm


code = pytest.main([sys.argv[1], "-q", "-p", "no:cacheprovider"], plugins=[Recorder()])
print(json.dumps({"code": int(code), "armed": armed}))
"""


def test_the_channel_reconnect_alarm_is_armed_only_after_the_sdk_is_loaded():
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, "-c", PROBE, CASE],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"code": 0, "armed": [{"stdio": True, "message": True}]}, (
        result.stdout + result.stderr
    )
