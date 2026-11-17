import subprocess
import time

from scripts import mcp_daemon


def wait_on_the_port_alone(deadline: float) -> int:
    # ruleid: serena-router-start-waits-on-an-exited-router
    while time.monotonic() < deadline:
        if mcp_daemon.port_open("127.0.0.1", 8643):
            return 0
        time.sleep(1)
    return 1


def wait_while_the_router_runs(router: subprocess.Popen[bytes], deadline: float) -> int:
    # ok: serena-router-start-waits-on-an-exited-router
    while time.monotonic() < deadline:
        if mcp_daemon.port_open("127.0.0.1", 8643):
            return 0
        if router.poll() is not None:
            return 1
        time.sleep(1)
    return 1
