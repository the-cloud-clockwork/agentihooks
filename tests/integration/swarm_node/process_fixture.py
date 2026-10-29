import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def herdr(args):
    root = Path(os.environ["SWARM_SUPERVISION_DIR"])
    path = "\0swarm-" + root.name
    if args == ["server"]:
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen()
        while True:
            connection, _ = server.accept()
            with connection:
                command = json.loads(connection.recv(65536))
                if command[:2] == ["pane", "run"]:
                    subprocess.Popen(["bash", "-c", command[3]], stdin=subprocess.DEVNULL)
                result = {"result": {"root_pane": {"pane_id": "fixture-pane"}}}
                if command[:2] == ["tab", "create"]:
                    result = {"error": "no workspace"}
                connection.sendall(json.dumps(result).encode())
    else:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
            client.sendall(json.dumps(args).encode())
            result = json.loads(client.recv(65536))
            if args[:2] != ["pane", "run"] or "error" in result:
                print(json.dumps(result))
            if "error" in result:
                raise SystemExit(1)


def process(role, mode):
    from scripts.swarm_v2.supervision_protocol import context, write

    root, _ = context()
    write(root / f"fixture-{role}.json", {"pid": os.getpid(), "started": True})
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if role == "agent":
        subprocess.Popen([sys.executable, __file__, "tool", mode])
    elif role == "tool":
        for name in ("grandchild-one", "grandchild-two"):
            subprocess.Popen([sys.executable, __file__, name, mode], start_new_session=name.endswith("two"))
    if mode == "complete" and role == "agent":
        time.sleep(0.2)
        return 0
    if mode == "fail" and role == "agent":
        return 9
    while True:
        write(root / f"heartbeat-{role}.json", {"tick": time.monotonic()})
        time.sleep(0.02)


def exporter(delay, mode):
    from scripts.swarm_v2.supervision_protocol import acknowledge, context, write

    root, scope = context()
    acknowledge("exporter", pid=os.getpid(), status="ready")
    while not (root / "quiesced.json").exists():
        write(root / "heartbeat-exporter.json", {"tick": time.monotonic()})
        time.sleep(0.02)
    time.sleep(delay)
    if mode != "missing":
        attempt = root.parent.parent.parent
        manifest = attempt / "checkpoints" / root.name / "manifest.json"
        manifest.parent.mkdir(parents=True)
        body = {**scope, "status": "complete", "checkpoint_id": root.name, "source": "synthetic fixture"}
        write(manifest, body)
        fields = {
            "status": "complete",
            "manifest": str(manifest.relative_to(attempt)),
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
        if mode == "stale":
            fields["incarnation"] = "previous-launch"
        acknowledge("exporter.checkpoint", **fields)
    while True:
        time.sleep(0.1)


def main():
    role, *args = sys.argv[1:]
    if role == "herdr":
        herdr(args)
    elif role == "exporter":
        exporter(float(args[0]), args[1])
    else:
        return process(role, args[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
