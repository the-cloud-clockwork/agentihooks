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
from argparse import Namespace
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from scripts.swarm import naming, stack_init
from scripts.swarm.store import SwarmError


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks swarm init", description=__doc__)
    parser.add_argument("name", nargs="?", default="", help="the swarm name, which is also its ledger name")
    parser.add_argument("--plan", help="the accepted plan file; its title seeds the ledger and the name suggestions")
    parser.add_argument("--content", help="ledger content JSON as ledger new takes it; default one phase for the plan")
    parser.add_argument("--repo", help="the repository the swarm works; default the current directory")
    parser.add_argument("--template", default="", help="the swarm template for caps, models and lanes")
    parser.add_argument("--operator-asked", help="the operator's words asking for a swarm with fewer than three tasks")
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
    sources = [str(Path(args.plan).expanduser())] if args.plan else []
    phase = {"title": title, "description": "", "planning": "auto"}
    return {"title": title, "overview": "", "sources": sources, "phases": [phase]}


def refused(out: TextIO, paint: stack_init.Paint, problem: str, title: str, taken=()) -> None:
    print(paint(f"Swarm not created: {problem}.", "failed"), file=out)
    print(f"  Rule: {naming.SWARM_NAME_RULE}.", file=out)
    print(f"  Try: {' or '.join(naming.swarm_name_suggestions(title, taken))}", file=out)


def taken(store) -> set[str]:
    from scripts.swarm_ledger.repository import repository

    return set(store.slugs()) | {row["slug"] for row in repository.list_summaries()}


def new_ledger():
    from scripts.swarm.ledger_client import LEDGER_DIR

    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import new_ledger

    return new_ledger


def create(args: argparse.Namespace, content: dict, environ: Mapping[str, str], out: TextIO) -> str:
    """Create the swarm and its ledger, or find them made; an empty string when done, else why it was refused."""
    from scripts.doctor import priming
    from scripts.swarm import cli
    from scripts.swarm_ledger import ledger_creator, ledger_link

    store = cli.connect()
    if args.name in store.slugs():
        repo = store.config(args.name).repo
        if args.repo and os.path.abspath(os.path.expanduser(args.repo)) != repo:
            return f"{args.name} is taken by the swarm working {repo}"
        print(json.dumps({"swarm": args.name, "created": False, "state": store.config(args.name).state}), file=out)
        print(ledger_link.page_line(args.name), file=out)
        return ""
    tasks = ledger_creator.content_tasks(content)
    refusal = ledger_creator.swarm_refusal(environ) or (
        args.template != priming.TEMPLATE and ledger_creator.floor_refusal(environ, tasks, args.operator_asked)
    )
    if refusal:
        return refusal
    new_ledger().create(args.name, content, "swarm")
    cli.cmd_create(
        store,
        Namespace(
            slug=args.name,
            repo=args.repo or ".",
            template=args.template,
            max_eng_agents=None,
            max_ci_agents=None,
            max_plan_agents=None,
            operator_asked=args.operator_asked,
        ),
    )
    return ""


def main(argv: list[str]) -> int:
    return init(argv, os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)


def init(argv: list[str], environ: Mapping[str, str], run: stack_init.Run, opener: Callable, out: TextIO) -> int:
    args = parser().parse_args(argv)
    paint = stack_init.painter(out, environ)
    content = seed(args)
    title = plan_title(args.plan) or str(content.get("title", ""))
    if problem := naming.swarm_name_problem(args.name):
        refused(out, paint, problem, title)
        return 1
    if code := stack_init.init(["--timeout", str(args.timeout)], environ, run, opener, out, args.name):
        return code
    try:
        problem = create(args, content, environ, out)
    except SwarmError as exc:
        problem = str(exc)
    if problem:
        print(paint(f"Swarm not created: {problem}.", "failed"), file=out)
        return 1
    print(f"Swarm {args.name} is paused; start it only when you want it: agentihooks swarm {args.name} start", file=out)
    return 0


def check(argv: list[str]) -> int:
    """agentihooks swarm names check NAME [--plan FILE]: valid or invalid, the reason and suggestions."""
    from scripts.swarm import cli

    checker = argparse.ArgumentParser(prog="agentihooks swarm names check", description=check.__doc__)
    checker.add_argument("name")
    checker.add_argument("--plan", help="the plan file whose title seeds the suggestions")
    args = checker.parse_args(argv)
    used = taken(cli.connect())
    if problem := naming.swarm_name_problem(args.name, used):
        print(f"invalid: {problem}")
        print(f"  Rule: {naming.SWARM_NAME_RULE}.")
        title = plan_title(args.plan) or args.name.replace("-", " ")
        print(f"  Try: {' or '.join(naming.swarm_name_suggestions(title, used))}")
        return 1
    print(f"valid: {args.name}")
    return 0
