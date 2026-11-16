import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import nats as nats_py
import pytest
from nats.errors import Error as NatsError
from nats.js.errors import APIError

from scripts.events import nats

REQUIRED_ENV = "NATS_SERVER_REQUIRED"
UNMATCHED_MSG_ID = "never-a-last-msg-id"
WRONG_LAST_MSG_ID = 10070
CLUSTER = "replicas"
READY_STREAM = "CLUSTER_READY"


def binary() -> str:
    found = shutil.which("nats-server")
    if found is None:
        if os.environ.get(REQUIRED_ENV) == "1":
            pytest.fail("nats-server is required on PATH")
        pytest.skip("nats-server is not on PATH")
    return found


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def closed_url() -> str:
    return f"nats://127.0.0.1:{free_port()}"


def wait_listening(port: int, process: subprocess.Popen, deadline_s: float = 10) -> None:
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        if process.poll() is not None:
            raise RuntimeError(f"nats-server exited with {process.returncode}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"nats-server did not listen on {port}")


def wait_logged(log: Path, text: str, deadline_s: float = 10) -> None:
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        if log.exists() and text in log.read_text():
            return
        time.sleep(0.02)
    raise AssertionError(f"nats-server did not log {text!r} in {log}")


def _wait_ports(ports: Path, process: subprocess.Popen, deadline_s: float = 10) -> dict[str, list[str]]:
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        if process.poll() is not None:
            raise RuntimeError(f"nats-server exited with {process.returncode}")
        try:
            return json.loads(ports.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise RuntimeError("nats-server did not publish its listener")


def _wait_url(ports: Path, process: subprocess.Popen, deadline_s: float = 10) -> str:
    return _wait_ports(ports, process, deadline_s)["nats"][0]


def durables(bus) -> list[str]:
    return sorted(info.name for info in bus._loop.run(bus._js.consumers_info(nats.EVENT_STREAM)))


def remembers(bus, subject: str, stream: str, event_id: str) -> bool:
    headers = {nats.MSG_ID: event_id, "Nats-Expected-Last-Msg-Id": UNMATCHED_MSG_ID}
    try:
        ack = bus._loop.run(bus._js.publish(subject, b"", stream=stream, headers=headers))
    except APIError as error:
        if error.err_code != WRONG_LAST_MSG_ID:
            raise
        return False
    return ack.duplicate


def wait_forgotten(bus, subject: str, stream: str, event_id: str, deadline_s: float = 10) -> None:
    stop = time.monotonic() + deadline_s
    while remembers(bus, subject, stream, event_id):
        if time.monotonic() >= stop:
            pytest.fail(f"{stream} still remembers message id {event_id!r} after {deadline_s}s")
        time.sleep(0.01)


@contextmanager
def running(store: Path) -> Iterator[str]:
    executable = binary()
    ports = store.parent / f"{store.name}-ports"
    ports.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [executable, "-js", "-a", "127.0.0.1", "-p", "-1", "-sd", str(store), "--ports_file_dir", str(ports)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield _wait_url(ports / f"{Path(executable).name}_{process.pid}.ports", process)
    finally:
        process.terminate()
        process.wait(10)


def _replicated(url: str, size: int, deadline_s: float = 30) -> None:
    async def probe() -> None:
        client = await nats_py.connect(url)
        try:
            js = client.jetstream()
            stop = time.monotonic() + deadline_s
            while True:
                try:
                    await js.add_stream(name=READY_STREAM, subjects=[READY_STREAM.lower()], num_replicas=size)
                    await js.delete_stream(READY_STREAM)
                    return
                except NatsError:
                    if time.monotonic() >= stop:
                        raise
                    await asyncio.sleep(0.2)
        finally:
            await client.close()

    asyncio.run(probe())


@contextmanager
def cluster(root: Path, size: int = 3) -> Iterator[list[str]]:
    """Run `size` routed JetStream servers and yield their client urls once a stream can hold a replica on each."""
    executable = binary()
    ports = root / "ports"
    ports.mkdir(parents=True, exist_ok=True)
    processes: list[subprocess.Popen] = []
    urls: list[str] = []
    routes: list[str] = []
    try:
        for index in range(size):
            process = subprocess.Popen(
                [
                    executable,
                    "-js",
                    "-a",
                    "127.0.0.1",
                    "-p",
                    "-1",
                    "-n",
                    f"{CLUSTER}-{index}",
                    "-sd",
                    str(root / f"store-{index}"),
                    "--cluster_name",
                    CLUSTER,
                    "--cluster",
                    "nats://127.0.0.1:-1",
                    "--ports_file_dir",
                    str(ports),
                    *routes,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            listed = _wait_ports(ports / f"{Path(executable).name}_{process.pid}.ports", process)
            urls.append(listed["nats"][0])
            routes = routes or ["--routes", listed["cluster"][0]]
        _replicated(urls[0], size)
        yield urls
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(10)
