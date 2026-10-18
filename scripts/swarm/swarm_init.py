"""agentihooks swarm init NAME: bring up the swarm stack, then create the swarm and its ledger under NAME, paused.

The one way to create a swarm. NAME is one or two lowercase words joined by a dash and ending in swarm. Running it
again for the same swarm prints its ledger page and changes nothing; it never starts the swarm.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from scripts.swarm import naming, stack_init
from scripts.swarm.stack_env import BIND, read_env

CREATE = """
import json, sys
from scripts.swarm import cli
from scripts.swarm_ledger import ledger_size
from scripts.swarm_ledger.repository import repository

order = json.load(sys.stdin)
name, store = order["name"], cli.connect()
if name in store.slugs():
    print(json.dumps({"found": "swarm", "repo": store.config(name).repo}))
elif repository.exists(name) and ledger_size.size_of(repository.get_document(name)) != "swarm":
    print(json.dumps({"found": "ledger"}))
else:
    repository.create(name, order["content"], "swarm")
    sys.exit(cli.main([name, "create", "--repo", order["repo"], *order["flags"]]))
"""
TAKEN = """
import json
from scripts.swarm import cli
from scripts.swarm_ledger import ledger_size
from scripts.swarm_ledger.repository import repository

held = {row["slug"] for row in repository.list_summaries() if ledger_size.size_of(row) != "swarm"}
print(json.dumps(sorted(set(cli.connect().slugs()) | held)))
"""


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks swarm init", description=__doc__)
    parser.add_argument("name", nargs="?", default="", help="the swarm name, which is also its ledger name")
    parser.add_argument("--plan", help="the accepted plan file; its title seeds the ledger and the name suggestions")
    parser.add_argument("--content", help="ledger content JSON as ledger new takes it; default one phase for the plan")
    parser.add_argument("--repo", help="the repository the swarm works; default the current directory")
    parser.add_argument("--template", default="", help="the swarm template for caps, models and lanes")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for every service to be healthy")
    return parser


def plan_title(plan: str | None) -> str:
    if not plan:
        return ""
    path = Path(plan).expanduser()
    heading = next((line[2:] for line in path.read_text().splitlines() if line.startswith("# ")), "")
    return heading.strip() or path.stem.replace("-", " ").replace("_", " ")


def seed(args: argparse.Namespace) -> dict:
    if args.content:
        return json.loads(Path(args.content).expanduser().read_bytes())
    title = plan_title(args.plan) or args.name.replace("-", " ")
    return {"title": title, "overview": "", "phases": [{"title": title, "description": "", "planning": "auto"}]}


def rule(out: TextIO, title: str, used=()) -> None:
    print(f"  Rule: {naming.SWARM_NAME_RULE}.", file=out)
    print(f"  Try: {' or '.join(naming.swarm_name_suggestions(title, used))}", file=out)


def in_stack(run: stack_init.Run, environ: Mapping[str, str], script: str, order: dict) -> subprocess.CompletedProcess:
    """Run SCRIPT with the stack's own code, ledger folder and Redis, so the swarm and its ledger live in the stack."""
    home = stack_init.stack_home(environ)
    argv = stack_init.compose(home, "exec", "-T", "swarm", "python", "-c", script)
    return run(argv, input=json.dumps(order), capture_output=True, text=True)


def taken(run: stack_init.Run, environ: Mapping[str, str]) -> set[str] | None:
    """The names the stack holds, or None when it does not answer."""
    done = in_stack(run, environ, TAKEN, {})
    return set(json.loads(done.stdout)) if done.returncode == 0 else None


def ledger_page(environ: Mapping[str, str], name: str) -> str:
    port = read_env(stack_init.stack_home(environ) / ".env")["SWARM_PORT"]
    return f"Ledger page: http://{BIND}:{port}/{name} (open it to follow and steer the work)"


def create(args: argparse.Namespace, content: dict, environ: Mapping[str, str], run: stack_init.Run, out: TextIO):
    """Create the swarm and its ledger in the stack, or find them made: (why it was refused, the name is taken)."""
    from scripts.swarm_ledger import ledger_creator

    if refusal := ledger_creator.creator_refusal(environ):
        return refusal, False
    repo = os.path.abspath(os.path.expanduser(args.repo or "."))
    flags = ["--template", args.template] if args.template else []
    stacked = {key: value for key, value in content.items() if key != "sources"}
    done = in_stack(run, environ, CREATE, {"name": args.name, "content": stacked, "repo": repo, "flags": flags})
    lines = done.stdout.strip().splitlines()
    if done.returncode:
        said = (done.stderr.strip().splitlines() or lines or ["no output"])[-1]
        return f"the stack refused it: {said}", False
    found = json.loads(lines[0]) if lines else {}
    if found.get("found") == "ledger":
        return f"{args.name} is taken by a ledger that is not a swarm's", True
    if found.get("found") == "swarm":
        if args.repo and repo != found["repo"]:
            return f"{args.name} is taken by the swarm working {found['repo']}", True
        print(json.dumps({"swarm": args.name, "created": False, "repo": found["repo"]}), file=out)
        print(ledger_page(environ, args.name), file=out)
        return "", False
    for line in lines:
        print(ledger_page(environ, args.name) if line.startswith("Ledger page:") else line, file=out)
    return "", False


def main(argv: list[str]) -> int:
    return init(argv, os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)


def init(argv: list[str], environ: Mapping[str, str], run: stack_init.Run, opener: Callable, out: TextIO) -> int:
    args = parser().parse_args(argv)
    paint = stack_init.painter(out, environ)
    content = seed(args)
    title = plan_title(args.plan) or str(content.get("title", ""))
    if problem := naming.swarm_name_problem(args.name):
        print(paint(f"Swarm not created: {problem}.", "failed"), file=out)
        rule(out, title)
        return 1
    if code := stack_init.init(["--timeout", str(args.timeout)], environ, run, opener, out, args.name):
        return code
    problem, name_taken = create(args, content, environ, run, out)
    if problem:
        print(paint(f"Swarm not created: {problem}.", "failed"), file=out)
        if name_taken:
            rule(out, title, (taken(run, environ) or set()) | {args.name})
        return 1
    print(f"Swarm {args.name} is paused; start it only when you want it: agentihooks swarm {args.name} start", file=out)
    return 0


def check(argv: list[str], environ: Mapping[str, str] = os.environ, run: stack_init.Run = subprocess.run) -> int:
    """agentihooks swarm names check NAME [--plan FILE]: valid or invalid, the reason and suggestions."""
    checker = argparse.ArgumentParser(prog="agentihooks swarm names check", description=check.__doc__)
    checker.add_argument("name")
    checker.add_argument("--plan", help="the plan file whose title seeds the suggestions")
    args = checker.parse_args(argv)
    used = taken(run, environ)
    if problem := naming.swarm_name_problem(args.name, used or ()):
        print(f"invalid: {problem}")
        rule(sys.stdout, plan_title(args.plan) or args.name.replace("-", " "), used or ())
        return 1
    unchecked = "" if used is not None else "; the swarm stack did not answer, so the names it holds were not checked"
    print(f"valid: {args.name}{unchecked}")
    return 0
