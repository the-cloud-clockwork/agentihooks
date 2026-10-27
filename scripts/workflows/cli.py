"""agentihooks workflow list | show <name> | validate <file|name>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import yaml

from scripts.workflows import exit_checks, loader, schema, store
from scripts.workflows.launchers import HeadlessProfile
from scripts.workflows.runner import Runner

PROG = "agentihooks workflow"
DESCRIPTION = "Workflows loaded from the package, bundle, profile and project layers"
SUBCOMMANDS = {"dest": "action", "required": True}
COMMANDS = {
    "list": ("Every workflow with the layer and file it loads from", None),
    "show": ("One workflow with its defaults filled in", "name"),
    "validate": ("Check a workflow file or a loaded name", "target"),
    "run": ("Run a workflow with a case file", "name"),
    "status": ("Read the saved state of a run", "run"),
    "resume": ("Continue a saved run", "run"),
}
BLOCK_DUMP = {"default_flow_style": False, "sort_keys": False}


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    actions = out.add_subparsers(**SUBCOMMANDS)
    for action, (text, argument) in COMMANDS.items():
        command = actions.add_parser(action, help=text)
        if argument:
            command.add_argument(argument)
        if action == "run":
            command.add_argument("--case", required=True, type=Path)
            command.add_argument("--run-id")
            command.add_argument("--harness", choices=("auto", "claude", "codex"), default="auto")
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


def run_folder(identifier: str) -> Path:
    if not identifier or identifier in (".", "..") or Path(identifier).name != identifier:
        raise ValueError("run identifier must be a single name")
    return store.default_root() / identifier


def execute(folder: Path, state: dict) -> dict:
    definition = schema.parse(yaml.safe_dump(state["definition"]))
    launcher = HeadlessProfile(harness=state["harness"])
    return Runner(definition, launcher, exit_checks.check).run(folder)


def run_workflow(args: argparse.Namespace) -> dict:
    found, errors = loader.resolve(Path.cwd())
    if errors:
        raise ValueError("\\n".join(errors))
    if args.name not in found:
        raise ValueError(f"no workflow named {args.name}")
    definition = found[args.name].workflow
    folder = run_folder(args.run_id if args.run_id is not None else uuid4().hex)
    case = args.case.read_text()
    state = store.create(folder, definition, case)
    state.update(definition=definition.to_document(), harness=args.harness)
    store.save(folder, state)
    return execute(folder, state)


def status(args: argparse.Namespace) -> dict:
    return store.load(run_folder(args.run))


def resume(args: argparse.Namespace) -> dict:
    folder = run_folder(args.run)
    return execute(folder, store.load(folder))


ACTIONS = {"list": list_workflows, "show": show, "validate": validate}
RUN_ACTIONS = {"run": run_workflow, "status": status, "resume": resume}


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    if args.action not in RUN_ACTIONS:
        return ACTIONS[args.action](args)
    try:
        state = RUN_ACTIONS[args.action](args)
    except (OSError, ValueError, schema.WorkflowError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(state))
    return 0 if args.action == "status" or state["status"] == "passed" else 1
