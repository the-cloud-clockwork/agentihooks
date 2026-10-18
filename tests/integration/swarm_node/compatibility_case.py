import json
import os
import sys
import time
import uuid
from pathlib import Path

from scripts.swarm_v2.worker_home import Request, bootstrap


def write(path, body):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(body))
    temporary.replace(path)


def main():
    if sys.argv[1] == "agent":
        attempt = Path(sys.argv[2])
        while True:
            write(attempt / "run/compatibility-agent.json", {"pid": os.getpid(), "tick": time.monotonic()})
            time.sleep(0.05)
    identifier = "exe-" + uuid.uuid4().hex
    root = Path("/home/worker/attempts")
    root.mkdir(mode=0o700)
    bootstrap(
        Request(
            root,
            identifier,
            Path("/opt/fixture/profiles"),
            {"codex": "fixture-codex"},
            Path("/opt/venv/bin/python"),
            {},
            {},
            10001,
            10001,
        )
    )
    attempt = root / identifier
    write(
        Path("/home/worker/current.json"),
        {"attempt": str(attempt), "actor": "fixture operator", "mode": "prior worker headless herdr compatibility"},
    )
    environment = dict(os.environ)
    environment.update(
        HOME=str(attempt / "homes/codex"),
        XDG_RUNTIME_DIR=str(attempt / "tmp"),
        HERDR_CONFIG_PATH=str(attempt / "run/herdr.toml"),
    )
    os.execvpe("herdr", ["herdr", "server"], environment)


if __name__ == "__main__":
    main()
