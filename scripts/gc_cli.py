import argparse
import json
import os
import sys
import time
from pathlib import Path

from hooks.lifecycle.act import ActionError
from hooks.lifecycle.config import load_roots
from hooks.lifecycle.evict import make_room
from hooks.lifecycle.lease import add_holder, admin_dir
from hooks.lifecycle.liveness import owner_holder, take_snapshot
from hooks.lifecycle.model import ACTIONABLE, Root
from hooks.lifecycle.run import state_home, sweep
from hooks.lifecycle.scratch_rm import remove_scratch
from hooks.lifecycle.timer import install_timer, remove_timer
from scripts import herdr_gc
from scripts.host_guard import sync as host_guard
from scripts.swarm import naming

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
    gc.add_argument(
        "--make-room", default="", metavar="REPO", help="Evict the oldest safe worktrees until wt.sh new may create"
    )
    gc.add_argument("--root", default=str(Path.home() / "dev" / "worktrees"), help="Worktree root for --make-room")
    gc.add_argument("--min-free-gb", type=int, default=50, help="Free space floor for --make-room")
    gc.add_argument("--max-per-repo", type=int, default=60, help="Per repository worktree cap for --make-room")
    gc.add_argument(
        "--install-timer", action="store_true", help="Schedule the hourly sweep on the agentihooks scheduler"
    )
    gc.add_argument(
        "--remove-timer", action="store_true", help="Remove the hourly sweep from the agentihooks scheduler"
    )
    lease = sub.add_parser("lease", help="Record the calling agent session as owner of a worktree or scratch dir")
    lease.add_argument("path")
    lease.add_argument("--kind", choices=("worktree", "ephemeral", "scratch"), default="worktree")
    scratch = sub.add_parser("scratch", help="Create or remove a leased scratch dir under ~/scratchpad")
    scratch.add_argument("action", choices=("new", "rm"))
    scratch.add_argument("name", nargs="?", default="", help="new: built from the session; rm: path of the task dir")
    name = sub.add_parser("name", help="Print the code built name for a new worktree, or check a given one")
    name.add_argument("kind", choices=("worktree", "tmp"))
    name.add_argument("--repo", default=".", help="Any checkout of the repository")
    name.add_argument("--check", default="", help="Exit 1 unless this is a built worktree name for the session")
    guard = sub.add_parser("host-guard", help="Link the host process guard to this checkout, or report its drift")
    guard.add_argument("action", choices=("install", "check"))
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
    for item in report.get("git_maintenance", []):
        state = item["outcome"] or ("maintenance registered" if item["registered"] else "maintenance not registered")
        print(f"git       {item['loose']!s:>5} loose {item['packs']!s:>5} packs  {item['path']}  ({state})")


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
    try:
        built = naming.scratch(os.environ, os.getcwd())
    except naming.NamingError:
        built = ""
    if built and name and name != built:
        print(
            f"scratch: names come from code; this session's task folder is {built} (run: scratch new)", file=sys.stderr
        )
        return 1
    name = built or name
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or ".." in parts or len(parts) > 3:
        print("scratch: name must be <repo>/<task> under ~/scratchpad", file=sys.stderr)
        return 1
    path = Path.home() / "scratchpad" / name
    path.mkdir(parents=True, exist_ok=True)
    _lease(path, "scratch")
    print(path)
    return 0


def _name(kind: str, repo: str, check: str) -> int:
    folder = Path(os.environ.get("WORKTREE_ROOT", Path.home() / "dev" / "worktrees")) / naming.repo_name(repo)
    folder = folder / "_tmp" if kind == "tmp" else folder
    taken = {path.name for path in folder.iterdir()} if folder.is_dir() else set()
    try:
        built = naming.tmp_worktree(os.environ, taken) if kind == "tmp" else naming.worktree(os.environ, taken)
        if check and not naming.is_worktree(check, os.environ, tmp=kind == "tmp"):
            print(
                f"name: names come from code; use {built} (run wt.sh {'tmp' if kind == 'tmp' else 'new'} without a name)",
                file=sys.stderr,
            )
            return 1
    except naming.NamingError as error:
        print(f"name: {error}", file=sys.stderr)
        return 3
    if not check:
        print(built)
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
    if args.command == "name":
        return _name(args.kind, args.repo, args.check)
    if args.command == "host-guard":
        return host_guard.run(args.action, Path.home())
    if args.install_timer or args.remove_timer:
        root = str(Path(__file__).resolve().parents[1])
        print(install_timer(sys.executable, root) if args.install_timer else remove_timer())
        return 0
    if args.make_room:
        root = Root("worktrees", str(Path(args.root).expanduser()), "worktrees")
        home = state_home()
        home.mkdir(parents=True, exist_ok=True)
        lines = make_room(Path(args.make_room), root, args.min_free_gb, args.max_per_repo, home, take_snapshot())
        for line in lines:
            print(f"wt: {line}")
        return 0
    herdr = herdr_gc.run(dict(os.environ), int(time.time() * 1000), args.enforce)
    if not args.json:
        for line in herdr:
            print(f"herdr: {line}")
    report = sweep(scope=str(Path(args.path).resolve()) if args.path else "", act=args.enforce)
    if args.json:
        print(json.dumps({**report, "herdr": herdr}, indent=2))
        return 0
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
