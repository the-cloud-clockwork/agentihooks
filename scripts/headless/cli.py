"""agentihooks headless run PROFILE --input TEXT|FILE [--schema FILE] [--harness] [--out DIR]."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from scripts.headless import profile_run

PROG = "agentihooks headless"
DESCRIPTION = "Run a full rendered profile unattended and print its result as JSON"
HARNESSES = ("auto", "claude", "codex")
TIERS = ("high", "low")
DEFAULT_TIMEOUT = 1800.0


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    actions = out.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="Run one profile on one input")
    run.add_argument("profile")
    run.add_argument("--input", required=True, help="Prompt text, or a file holding it")
    run.add_argument("--schema", type=Path, help="JSON schema file the output must match")
    run.add_argument("--harness", choices=HARNESSES, default="auto")
    run.add_argument("--out", type=Path, help="Run folder for the transcript")
    run.add_argument("--tier", choices=TIERS, default="high")
    run.add_argument("--effort", default="medium")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Seconds before the run is killed")
    return out


def default_run_dir(environ: Mapping[str, str]) -> Path:
    home = Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return home / "headless" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def read_input(value: str) -> str:
    try:
        return Path(value).read_text()
    except OSError:
        return value


def read_schema(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"no schema file {path}")
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"schema {path} is not JSON: {exc}") from exc


def run(args: argparse.Namespace) -> int:
    try:
        schema = None if args.schema is None else read_schema(args.schema)
    except ValueError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    run_dir = args.out or default_run_dir(os.environ)
    request = profile_run.ProfileRun(
        profile=args.profile,
        harness=args.harness,
        input=read_input(args.input),
        schema=schema,
        tier=args.tier,
        effort=args.effort,
        timeout=args.timeout,
        run_dir=run_dir,
    )
    result = profile_run.run_profile(request)
    report = {
        "status": result.status,
        "output": result.output,
        "session": result.session,
        "exit_code": result.exit_code,
        "transcript": str(result.transcript),
        "run_dir": str(run_dir),
    }
    print(json.dumps(report))
    return 0 if result.status == "ok" else 1


def main(argv: list[str]) -> int:
    return run(parser().parse_args(argv))
