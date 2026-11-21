import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scripts.scheduler.process_timer import POLL_S
from scripts.scheduler.service import PASS_S
from scripts.swarm_ledger.events.stream import parse
from scripts.swarm_ledger.ledger_server import FOLLOW_S
from tests.ledger_load import gate

SECONDS = 30
WRITES_PER_SECOND = 2
PUBLISH_MARGIN_S = 1.0
FOLLOW_PASS_S = PASS_S + FOLLOW_S + POLL_S + PUBLISH_MARGIN_S
DELIVERY_MARGIN_S = 0.25
DELIVERY_BOUND_S = FOLLOW_S + DELIVERY_MARGIN_S
DELIVERY_POLL_S = 0.05


def _wait_listening(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + gate.SERVER_WAIT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("server did not listen")


@contextmanager
def serving(
    source: Path, folder: Path, write_port: int, read_port: int
) -> Iterator[tuple[subprocess.Popen, subprocess.Popen]]:
    env = {
        **os.environ,
        "LEDGER_DIR": str(folder),
        "SWARM_RELOAD": "0",
        "CONDITIONS_TRUSTED_OWNERS": "The-Cloud-Clockwork",
    }
    write_env = {**env, "LEDGER_API_HOST": "127.0.0.1", "LEDGER_API_PORT": str(write_port)}
    read_env = {**env, "LEDGER_PORT": str(read_port)}
    with (folder / "server.log").open("w") as log, (folder / "server-read.log").open("w") as read_log:
        write_process = subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm_ledger.api_service", "--serve"],
            cwd=source,
            env=write_env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        read_process = subprocess.Popen(
            [sys.executable, str(source / "scripts/swarm_ledger/ledger_server.py"), "--serve"],
            cwd=source,
            env=read_env,
            stdout=read_log,
            stderr=read_log,
            stdin=subprocess.DEVNULL,
        )
        try:
            _wait_listening(write_process, write_port)
            _wait_listening(read_process, read_port)
            yield write_process, read_process
        finally:
            gate.stop_server(read_process)
            gate.stop_server(write_process)


def consume(base: str, token: str, ready: threading.Event, stop: threading.Event, result: dict) -> None:
    request = urllib.request.Request(
        f"{base}/api/v1/ledgers/{gate.SLUG}/events",
        headers={"X-Ledger-Token": token, "Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=SECONDS + gate.TIMEOUT_S) as response:
            for name, data, _ in parse(line.decode("utf-8") for line in response):
                if name == "snapshot":
                    result["snapshots"] += 1
                    ready.set()
                elif name == "ledger":
                    result["patches"] += 1
                    result["patch_bytes"] += len(json.dumps(data).encode("utf-8"))
                if stop.is_set():
                    return
    except (OSError, ValueError) as exc:
        if not stop.is_set():
            result["errors"].append(str(exc))
    finally:
        ready.set()


def measure(source: Path, folder: Path, doc: dict, streams: int) -> dict:
    folder.mkdir(parents=True)
    token = gate.store(folder, doc)
    write_port, read_port = gate.free_port(), gate.free_port()
    write_base, read_base = f"http://127.0.0.1:{write_port}", f"http://127.0.0.1:{read_port}"
    consumers = [{"snapshots": 0, "patches": 0, "patch_bytes": 0, "errors": []} for _ in range(streams)]
    ready = [threading.Event() for _ in consumers]
    stop = threading.Event()
    threads = [
        threading.Thread(target=consume, args=(read_base, token, event, stop, result), daemon=True)
        for event, result in zip(ready, consumers, strict=True)
    ]
    writes = []
    with serving(source, folder, write_port, read_port) as (write_server, read_server):
        credentials = {
            "X-Ledger-Token": gate.authority.agent_token(token, gate.SLUG, "ci@ab0000-0000"),
            "X-Ledger-Agent": "ci@ab0000-0000",
        }
        api = gate.Front(write_base, read_base, credentials)
        api.mutate(gate.SLUG, [{"op": "join", "id": "stream-load-join", "by": "ci@ab0000-0000", "role": "member"}])
        try:
            for thread in threads:
                thread.start()
            for event, result in zip(ready, consumers, strict=True):
                if not event.wait(gate.SERVER_WAIT_S) or result["snapshots"] != 1:
                    raise RuntimeError(f"stream did not receive its snapshot: {result}")
            began, initial_cpu = time.monotonic(), gate.cpu_seconds(write_server.pid)
            for n in range(SECONDS * WRITES_PER_SECOND):
                time.sleep(max(0, began + n / WRITES_PER_SECOND - time.monotonic()))
                sent = time.monotonic()
                reply = api.mutate(
                    gate.SLUG,
                    [
                        {
                            "op": "add",
                            "thread": "tasks/t0/comments",
                            "id": f"stream-load-{n}",
                            "by": "ci@ab0000-0000",
                            "text": f"Stream load write {n} landed.",
                        }
                    ],
                )
                if (reply.get("data") or reply).get("rejected"):
                    raise RuntimeError(f"write {n} was rejected")
                writes.append(time.monotonic() - sent)
            last_write = time.monotonic() - began
            time.sleep(max(0, began + SECONDS - time.monotonic()))
            elapsed = time.monotonic() - began
            cpu = gate.cpu_seconds(write_server.pid) - initial_cpu
            deadline = time.monotonic() + gate.TIMEOUT_S
            while time.monotonic() < deadline and any(
                result["patches"] < len(writes) and not result["errors"] for result in consumers
            ):
                time.sleep(DELIVERY_POLL_S)
            delivered = time.monotonic() - began
            if any(result["patches"] != len(writes) or result["errors"] for result in consumers):
                raise RuntimeError(
                    f"streams did not receive every write {gate.TIMEOUT_S:g} s after the window, last write at"
                    f" {last_write:.2f} s, waited to {delivered:.2f} s: {consumers}"
                )
            if delivered - last_write > FOLLOW_PASS_S:
                raise RuntimeError(
                    f"streams received the last write {delivered - last_write:.2f} s after it, last write at"
                    f" {last_write:.2f} s, delivered at {delivered:.2f} s, past one follow pass of {FOLLOW_PASS_S:g} s"
                )
            return {
                "streams": streams,
                "writes": len(writes),
                "seconds": elapsed,
                "cpu_seconds": cpu,
                "cpu_cores": cpu / elapsed,
                "write_p95_s": gate.p95(writes),
                "last_write_s": last_write,
                "delivered_s": delivered,
                "consumers": consumers,
            }
        finally:
            stop.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=gate.TIMEOUT_S)


def compare(base: Path, head: Path, folder: Path) -> dict:
    doc = gate.full_size(gate.core.now_ms())
    fixture = json.dumps(doc, sort_keys=True).encode("utf-8")
    result = {"fixture_sha256": hashlib.sha256(fixture).hexdigest(), "ledger_bytes": len(fixture), "cases": []}
    for streams in (1, 3):
        before = measure(base, folder / f"before-{streams}", doc, streams)
        after = measure(head, folder / f"after-{streams}", doc, streams)
        result["cases"].append({"before": before, "after": after})
        print(json.dumps(result["cases"][-1]), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("stream load measurement runs in CI only")
    args.folder.mkdir(parents=True, exist_ok=True)
    result = compare(args.base.resolve(), args.head.resolve(), args.folder)
    (args.folder / "streams.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return int(any(case["after"]["cpu_cores"] >= case["before"]["cpu_cores"] for case in result["cases"]))


if __name__ == "__main__":
    sys.exit(main())
