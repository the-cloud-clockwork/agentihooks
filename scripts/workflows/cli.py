"""agentihooks workflow list | show <name> | validate <file|name>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from scripts.workflows import loader, schema

PROG = "agentihooks workflow"


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog=PROG, description="Workflows loaded from the package, bundle, profile and project layers"
    )
    actions = out.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="Every workflow with the layer and file it loads from")
    actions.add_parser("show", help="One workflow with its defaults filled in").add_argument("name")
    actions.add_parser("validate", help="Check a workflow file or a loaded name").add_argument("target")
    return out


def refuse(message: str) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return 1


def report(errors: list[str]) -> int:
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def list_workflows(args: argparse.Namespace) -> int:
    found, errors = loader.resolve(Path.cwd())
    for name in sorted(found):
        print(f"{name}\t{found[name].layer}\t{found[name].path}")
    return report(errors)


def show(args: argparse.Namespace) -> int:
    found, errors = loader.resolve(Path.cwd())
    report(errors)
    if args.name not in found:
        return refuse(f"no workflow named {args.name}")
    entry = found[args.name]
    print(f"# {entry.layer}: {entry.path}")
    print(yaml.safe_dump(entry.workflow.to_document(), default_flow_style=False, sort_keys=False), end="")
    return 0


def validate(args: argparse.Namespace) -> int:
    path = Path(args.target)
    if path.is_file():
        try:
            workflow = loader.read(path)
        except schema.WorkflowError as exc:
            return report([f"{path}: {error}" for error in exc.errors])
        print(f"valid: {workflow.name} ({path})")
        return 0
    found, errors = loader.resolve(Path.cwd())
    if args.target not in found:
        report(errors)
        return refuse(f"no workflow named {args.target}")
    print(f"valid: {args.target} ({found[args.target].path})")
    return report(errors)


ACTIONS = {"list": list_workflows, "show": show, "validate": validate}


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    return ACTIONS[args.action](args)
