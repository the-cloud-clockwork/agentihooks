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
from contextlib import contextmanager
from pathlib import Path

from scripts.swarm_ledger.events.stream import parse
from tests.ledger_load import gate

SECONDS = 30
WRITES_PER_SECOND = 2


@contextmanager
def serving(source: Path, folder: Path, port: int):
    env = {**os.environ, "LEDGER_DIR": str(folder), "LEDGER_PORT": str(port), "SWARM_RELOAD": "0"}
    with (folder / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(source / "scripts/swarm_ledger/ledger_server.py"), "--serve"],
            cwd=source,
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + gate.SERVER_WAIT_S
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"server exited with {process.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise TimeoutError("server did not listen")
            yield process
        finally:
            gate.stop_server(process)


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
    port = gate.free_port()
    base = f"http://127.0.0.1:{port}"
    consumers = [{"snapshots": 0, "patches": 0, "patch_bytes": 0, "errors": []} for _ in range(streams)]
    ready = [threading.Event() for _ in consumers]
    stop = threading.Event()
    threads = [
        threading.Thread(target=consume, args=(base, token, event, stop, result), daemon=True)
        for event, result in zip(ready, consumers, strict=True)
    ]
    writes = []
    with serving(source, folder, port) as server:
        credentials = {
            "X-Ledger-Token": gate.authority.agent_token(token, gate.SLUG, "ci@ab0000-0000"),
            "X-Ledger-Agent": "ci@ab0000-0000",
        }
        api = gate.ResourceClient(base, credentials, gate.TIMEOUT_S)
        api.mutate(gate.SLUG, [{"op": "join", "id": "stream-load-join", "by": "ci@ab0000-0000", "role": "member"}])
        try:
            for thread in threads:
                thread.start()
            for event, result in zip(ready, consumers, strict=True):
                if not event.wait(gate.SERVER_WAIT_S) or result["snapshots"] != 1:
                    raise RuntimeError(f"stream did not receive its snapshot: {result}")
            began, initial_cpu = time.monotonic(), gate.cpu_seconds(server.pid)
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
            time.sleep(max(0, began + SECONDS - time.monotonic()))
            elapsed = time.monotonic() - began
            cpu = gate.cpu_seconds(server.pid) - initial_cpu
            if any(result["patches"] != len(writes) or result["errors"] for result in consumers):
                raise RuntimeError(f"streams did not receive every write: {consumers}")
            return {
                "streams": streams,
                "writes": len(writes),
                "seconds": elapsed,
                "cpu_seconds": cpu,
                "cpu_cores": cpu / elapsed,
                "write_p95_s": gate.p95(writes),
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
