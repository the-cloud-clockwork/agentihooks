"""Write a drafted workflow in block style: the package folder inside an agentihooks checkout, the linked bundle elsewhere."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

import yaml

from hooks.context import profile_chain
from hooks.context.conditions import repo_root
from scripts.workflows import loader, schema

PROG = "write_workflow.py"
PACKAGE_NAME = "agentihooks"
PYPROJECT = "pyproject.toml"
PACKAGE_FOLDER = Path("profiles") / "package" / loader.WORKFLOWS
SUFFIX = ".yaml"
BLOCK_DUMP = {"default_flow_style": False, "sort_keys": False}
NO_BUNDLE = "no linked bundle: link one with agentihooks bundle link, or work inside an agentihooks checkout"
EXISTS = "{path} already exists: pass --replace to overwrite it"


class WriterError(ValueError):
    """A workflow cannot be written to its chosen folder."""


def project_name(root: Path) -> str | None:
    """The project name in the checkout's pyproject, or None when it names none."""
    try:
        document = tomllib.loads((root / PYPROJECT).read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = document.get("project")
    return project.get("name") if isinstance(project, dict) else None


def target(cwd: Path, state: dict, environ: Mapping[str, str]) -> Path:
    """The folder a new workflow goes to for a session working in *cwd*."""
    root = repo_root(cwd)
    if root is not None and project_name(root) == PACKAGE_NAME:
        return root / PACKAGE_FOLDER
    bundle = profile_chain.bundle_path(state, environ)
    if bundle is None:
        raise WriterError(NO_BUNDLE)
    return bundle / loader.WORKFLOWS


def write(draft: Path, cwd: Path, state: dict, environ: Mapping[str, str], replace: bool = False) -> Path:
    """Validate *draft* and write it, block style and named after the workflow, into the chosen folder."""
    workflow = loader.read(draft)
    path = target(cwd, state, environ) / f"{workflow.name}{SUFFIX}"
    if path.exists() and not replace:
        raise WriterError(EXISTS.format(path=path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(workflow.to_document(), **BLOCK_DUMP))
    return path


def parser() -> argparse.ArgumentParser:
    """Arguments: the draft file and the overwrite switch."""
    out = argparse.ArgumentParser(prog=PROG, description=__doc__)
    out.add_argument("draft", type=Path)
    out.add_argument("--replace", action="store_true")
    return out


def main(argv: list[str]) -> int:
    """Write the draft and print the written path as JSON; print every refusal and exit one."""
    args = parser().parse_args(argv)
    try:
        path = write(args.draft, Path.cwd(), profile_chain.read_state(), os.environ, args.replace)
    except schema.WorkflowError as exc:
        print("\n".join(f"{args.draft}: {error}" for error in exc.errors), file=sys.stderr)
        return 1
    except WriterError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"written": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
