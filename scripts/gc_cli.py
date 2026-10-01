import argparse
import json
import os
import sys
from pathlib import Path

from hooks.lifecycle.act import ActionError
from hooks.lifecycle.config import load_roots
from hooks.lifecycle.lease import add_holder, admin_dir
from hooks.lifecycle.liveness import owner_holder, take_snapshot
from hooks.lifecycle.model import ACTIONABLE
from hooks.lifecycle.run import sweep
from hooks.lifecycle.scratch_rm import remove_scratch

GB = 1 << 30


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentihooks", description="Workspace lifecycle: report, leases, scratch dirs."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gc = sub.add_parser("gc", help="Classify worktrees, scratch dirs and tool files; report what is safe to remove")
    gc.add_argument("--json", action="store_true", help="Print the full report as JSON")
    gc.add_argument("--path", default="", help="Only report findings under this path")
    gc.add_argument("--enforce", action="store_true", help="Act on findings that are due: remove, snapshot, archive")
    lease = sub.add_parser("lease", help="Record the calling agent session as owner of a worktree or scratch dir")
    lease.add_argument("path")
    lease.add_argument("--kind", choices=("worktree", "ephemeral", "scratch"), default="worktree")
    scratch = sub.add_parser("scratch", help="Create or remove a leased scratch dir under ~/scratchpad")
    scratch.add_argument("action", choices=("new", "rm"))
    scratch.add_argument("name", help="new: <repo>/<task>; rm: path of the task dir")
    return parser


def _print_report(report: dict) -> None:
    if "skipped" in report:
        print(f"gc: {report['skipped']}")
        return
    for action, bucket in sorted(report["totals"].items()):
        print(f"{action:9} {bucket['count']:5}  {bucket['bytes'] / GB:8.2f} GB")
    for item in report["findings"]:
        if item["action"] in ACTIONABLE or item["action"] == "skip":
            state = item.get("outcome") or ("due" if item["due"] else "pending")
            print(f"{item['action']:9} {state:8} {item['size'] / GB:7.2f} GB  {item['path']}  ({item['reason']})")


def _lease(path: Path, kind: str) -> int:
    if not path.is_dir():
        print(f"lease: no such directory: {path}", file=sys.stderr)
        return 1
    if kind != "scratch" and admin_dir(path) is None:
        print(f"lease: not a linked git worktree: {path}", file=sys.stderr)
        return 1
    snap = take_snapshot()
    holder = owner_holder(snap)
    if holder is None:
        print("lease: no agent session found among parent processes; nothing recorded", file=sys.stderr)
        return 0
    add_holder(path, kind, holder, snap.now)
    return 0


def _scratch_new(name: str) -> int:
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or ".." in parts or len(parts) > 3:
        print("scratch: name must be <repo>/<task> under ~/scratchpad", file=sys.stderr)
        return 1
    path = Path.home() / "scratchpad" / name
    path.mkdir(parents=True, exist_ok=True)
    _lease(path, "scratch")
    print(path)
    return 0


def _scratch_rm(name: str) -> int:
    path = Path(name).expanduser().resolve()
    try:
        remove_scratch(path, load_roots(), take_snapshot(), os.getppid())
    except (ActionError, OSError) as error:
        print(f"scratch rm: refused: {error}", file=sys.stderr)
        return 1
    print(f"removed {path}")
    return 0


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.command == "lease":
        return _lease(Path(args.path).resolve(), args.kind)
    if args.command == "scratch":
        return _scratch_new(args.name) if args.action == "new" else _scratch_rm(args.name)
    report = sweep(scope=str(Path(args.path).resolve()) if args.path else "", act=args.enforce)
    print(json.dumps(report, indent=2)) if args.json else _print_report(report)
    return 0
