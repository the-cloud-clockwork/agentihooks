"""Advance one workflow in a detached process holding its workspace lock."""

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.inbox.store import connect
from scripts.swarm import workflow_runner


def start(request: Mapping[str, Any]) -> bool:
    folder = Path(request["task"]["workspace"])
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "workflow.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        payload = folder / "workflow-task.json"
        payload.write_text(json.dumps({"task": request["task"], "repo": request["repo"]}))
        with (folder / "workflow-job.log").open("ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "scripts.swarm.workflow_job", str(lock.fileno()), str(payload)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=Path(__file__).resolve().parents[2],
                start_new_session=True,
                pass_fds=(lock.fileno(),),
            )
    return True


def main(argv: list[str]) -> int:
    with os.fdopen(int(argv[0])):
        request = json.loads(Path(argv[1]).read_text())
        print(workflow_runner.advance_task(connect(), request["task"], Path(request["repo"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
