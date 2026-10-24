"""agentihooks workflow list | show <name> | validate <file|name>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from scripts.workflows import loader, schema

PROG = "agentihooks workflow"
DESCRIPTION = "Workflows loaded from the package, bundle, profile and project layers"
SUBCOMMANDS = {"dest": "action", "required": True}
COMMANDS = {
    "list": ("Every workflow with the layer and file it loads from", None),
    "show": ("One workflow with its defaults filled in", "name"),
    "validate": ("Check a workflow file or a loaded name", "target"),
}
BLOCK_DUMP = {"default_flow_style": False, "sort_keys": False}


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    actions = out.add_subparsers(**SUBCOMMANDS)
    for action, (text, argument) in COMMANDS.items():
        command = actions.add_parser(action, help=text)
        if argument:
            command.add_argument(argument)
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
    if args.name not in found:
        report(errors)
        return refuse(f"no workflow named {args.name}")
    entry = found[args.name]
    sys.stdout.write(f"# {entry.layer}: {entry.path}\n" + yaml.safe_dump(entry.workflow.to_document(), **BLOCK_DUMP))
    return report(errors)


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
