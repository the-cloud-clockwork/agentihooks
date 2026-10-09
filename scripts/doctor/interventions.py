"""The only ways the Doctor touches the swarm it watches; each is logged on both ledgers. Anything else is refused."""

import ast
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from scripts.inbox.seats import of_swarm, seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import control_notifications
from scripts.swarm.ledger_client import LEDGER_DIR
from scripts.swarm.store import MASTER, SwarmError

NEVER = "The Doctor never changes the watched swarm's tasks or kills an agent mid work."
HANDOFF_ASK = (
    "The Doctor applied a fix that reaches you only in a fresh session. At your next stop, once the step you are on "
    "lands, write your handoff document and a recap, run agentihooks swarm {slug} handoff <doc> --recap <recap> and "
    "stop. A successor continues your task."
)


def _pidfile():
    return Path(os.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser() / ".server.pid"


@dataclass(frozen=True)
class Context:
    store: object
    ledger: object
    watched: str
    doctor: str
    run: Callable = subprocess.run
    code: Path = LEDGER_DIR
    pidfile: Path = field(default_factory=_pidfile)


def _call(ctx, argv):
    done = ctx.run(argv, capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise SwarmError(f"{' '.join(argv)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def pull_dev(ctx, args):
    repo = ctx.store.config(ctx.watched).repo
    branch = _call(ctx, ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch != "dev":
        raise SwarmError(f"the main checkout {repo} is on {branch}, not dev; the Doctor pulls only into dev")
    _call(ctx, ["git", "-C", repo, "pull", "--ff-only", "origin", "dev"])
    return "The Doctor pulled dev into the main checkout of the watched swarm."


def _module_files(root, dotted):
    files, base = [], root
    for part in dotted.split("."):
        if (base / part / "__init__.py").is_file():
            base = base / part
            files.append(base / "__init__.py")
        elif (base / f"{part}.py").is_file():
            return [*files, base / f"{part}.py"]
        else:
            break
    return files


def _imported(node, path, roots):
    if isinstance(node, ast.Import):
        wanted = [(roots, alias.name) for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        bases = (path.parents[node.level - 1],) if node.level else roots
        prefix = f"{node.module}." if node.module else ""
        wanted = [(bases, prefix + alias.name) for alias in node.names]
    else:
        return []
    return [found for bases, dotted in wanted for root in bases for found in _module_files(root, dotted)]


def _server_modules(code):
    roots = (code, code.parents[1])
    seen, todo = set(), [code / "ledger_server.py"]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_bytes())):
            todo += _imported(node, path, roots)
    return seen


def _changed_since_start(ctx):
    pages = {p for p in ctx.code.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    started = ctx.pidfile.stat().st_mtime
    changed = (p for p in pages | _server_modules(ctx.code) if p.stat().st_mtime > started)
    return sorted(str(p.relative_to(ctx.code.parents[1])) for p in changed)


def restart_ledger_server(ctx, args):
    if ctx.pidfile.exists():
        changed = _changed_since_start(ctx)
        if not changed:
            raise SwarmError(
                "the ledger server already runs its current code; the Doctor restarts it only after a change"
            )
        why = f"{', '.join(changed)} changed since it started"
    else:
        why = "no running server recorded its start"
    for flag in ("--stop", "--ensure"):
        _call(ctx, [sys.executable, str(ctx.code / "ledger_server.py"), flag])
    return f"The Doctor restarted the ledger server on its new code: {why}."


def refresh_rules(ctx, args):
    _call(ctx, ["agentihooks", "refresh-rules"])
    return "The Doctor refreshed the installed rules for every running session."


def culture(ctx, args):
    try:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"cannot read the culture file {args.file}: {exc.strerror}") from exc
    ctx.store.culture.set(ctx.watched, text)
    return "The Doctor updated the culture of the watched swarm."


def _send(ctx, address, text):
    InboxStore(ctx.store.redis).send(seat_address(ctx.doctor, MASTER), address, text)


def handoff_at_stop(ctx, args):
    agent = next((a for a in ctx.store.agents(ctx.watched) if a.name == args.to and a.state != "finished"), None)
    if agent is None:
        raise SwarmError(f"no agent {args.to} works in the watched swarm {ctx.watched}")
    _send(ctx, agent.seat or agent.name, HANDOFF_ASK.format(slug=ctx.watched))
    return "The Doctor asked one agent of the watched swarm to hand off at its next stop."


def message(ctx, args):
    if not of_swarm(args.to, ctx.watched, ctx.store.names):
        raise SwarmError(f"{args.to} is not the master or an agent of the watched swarm {ctx.watched}")
    if not args.text.strip():
        raise SwarmError("a message needs its text")
    _send(ctx, args.to, args.text)
    who = "master" if args.to == seat_address(ctx.watched, MASTER) else "one agent"
    return f"The Doctor sent a message to the watched swarm's {who}."


ALLOWED = {
    "pull-dev": pull_dev,
    "restart-ledger-server": restart_ledger_server,
    "refresh-rules": refresh_rules,
    "culture": culture,
    "handoff-at-stop": handoff_at_stop,
    "message": message,
}


def apply(ctx, action, args):
    if action not in ALLOWED:
        raise SwarmError(f"{action} is not an allowed intervention; the Doctor may only {', '.join(ALLOWED)}. {NEVER}")
    line = ALLOWED[action](ctx, args)
    for slug in (ctx.watched, ctx.doctor):
        ctx.ledger.notify(slug, line)
    boss = control_notifications.master(ctx.store, ctx.watched)
    _send(ctx, (boss.seat or boss.name) if boss else seat_address(ctx.watched, MASTER), line)
    return line
