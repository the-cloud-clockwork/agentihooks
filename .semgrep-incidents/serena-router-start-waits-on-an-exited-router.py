import subprocess
import time

from scripts import mcp_daemon
from scripts.mcp_daemon import pid_alive, port_open


def wait_on_the_port_alone(deadline: float) -> int:
    while time.monotonic() < deadline:
        # ruleid: serena-router-start-waits-on-an-exited-router
        if mcp_daemon.port_open("127.0.0.1", 8643):
            return 0
        time.sleep(1)
    return 1


def count_down_on_the_port_alone(tries: int) -> int:
    for _ in range(tries):
        # ruleid: serena-router-start-waits-on-an-exited-router
        if port_open("127.0.0.1", 8643):
            return 0
        time.sleep(1)
    return 1


def wait_while_the_router_runs(router: subprocess.Popen[bytes], deadline: float) -> int:
    while time.monotonic() < deadline:
        # ok: serena-router-start-waits-on-an-exited-router
        if mcp_daemon.port_open("127.0.0.1", 8643):
            return 0
        if router.poll() is not None:
            return 1
        time.sleep(1)
    return 1


def count_down_while_the_router_lives(pid: int, tries: int) -> int:
    for _ in range(tries):
        if not mcp_daemon.pid_alive(pid):
            return 1
        # ok: serena-router-start-waits-on-an-exited-router
        if port_open("127.0.0.1", 8643):
            return 0
        time.sleep(1)
    return 1


def wait_while_the_pid_lives(pid: int, deadline: float) -> int:
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return 1
        # ok: serena-router-start-waits-on-an-exited-router
        if mcp_daemon.port_open("127.0.0.1", 8643):
            return 0
        time.sleep(1)
    return 1
