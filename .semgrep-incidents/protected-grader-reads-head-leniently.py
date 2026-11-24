import argparse
from pathlib import Path
from typing import Any

import yaml

Read = tuple[Path, str, type[yaml.SafeLoader]]


class ActionsLoader(yaml.SafeLoader):
    """Stands in for the grader's loader."""


def load(root: Path, rev: str, loader: type[yaml.SafeLoader] = yaml.SafeLoader) -> Read:
    return root, rev, loader


def read_with_the_default_loader(text: str) -> Any:
    # ruleid: protected-grader-reads-head-leniently
    return yaml.safe_load(text)


def read_with_the_safe_loader(text: str) -> Any:
    # ruleid: protected-grader-reads-head-leniently
    return yaml.load(text, Loader=yaml.SafeLoader)


def head_with_the_default_loader(root: Path, args: argparse.Namespace) -> Read:
    # ruleid: protected-grader-reads-head-leniently
    return load(root, args.head)


def head_with_another_loader(root: Path, args: argparse.Namespace) -> Read:
    # ruleid: protected-grader-reads-head-leniently
    return load(root, args.head, yaml.SafeLoader)


def head_as_actions_reads_it(root: Path, args: argparse.Namespace) -> Read:
    # ok: protected-grader-reads-head-leniently
    return load(root, args.head, ActionsLoader)


def base_as_pyyaml_reads_it(root: Path, args: argparse.Namespace) -> Read:
    # ok: protected-grader-reads-head-leniently
    return load(root, args.base)
