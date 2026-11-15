from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from hooks.secrets import redact
from scripts import herdr_host
from scripts.profiles import binding


def path_for(launcher: Path, environ: dict[str, str]) -> Path:
    home = Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME", str(Path.home()))) / ".agentihooks")
    return home / "session-exits" / f"{launcher.stem}.json"


def record_exit(path: Path, status: int, harness: str, name: str, environ: dict[str, str]) -> None:
    pane = environ.get("HERDR_PANE_ID", "")
    tail, error = "", "no herdr pane"
    if pane:
        try:
            result = herdr_host._cli(
                ["pane", "read", pane, "--source", "recent", "--lines", "40", "--format", "text"], environ
            )
            tail, error = result["text"], ""
        except (herdr_host.HerdrError, OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
    record = {
        "exit_status": status,
        "harness": harness,
        "name": name,
        "pane_id": pane,
        "pane_tail": redact(tail, mode="strict"),
        "pane_error": redact(error, mode="strict"),
        "ended_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_suffix(".pending")
    with pending.open("w", encoding="utf-8") as stream:
        pending.chmod(0o600)
        json.dump(record, stream)
    pending.replace(path)


def fail_pending_canary(status: int, harness: str, environ: dict[str, str]) -> None:
    """An agent that exits before its profile canary fails the launch wait now instead of at its deadline."""
    report = environ.get(binding.REPORT)
    if report and Path(report).is_file():
        binding.refuse(Path(report), f"the {harness} agent exited with status {status} before it reported its canary")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("status", type=int)
    parser.add_argument("harness")
    parser.add_argument("name")
    parser.add_argument("--viewer", action="store_true")
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    record_exit(args.path, args.status, args.harness, args.name, environ)
    if not args.viewer:
        fail_pending_canary(args.status, args.harness, environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
