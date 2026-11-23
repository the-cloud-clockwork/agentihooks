from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import subprocess
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import cli

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def records() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("parser_arguments.json").read_text())


def _reference(records: dict[str, Any]) -> Callable[[], argparse.ArgumentParser]:
    namespace = vars(cli).copy()
    exec(compile(records["builder"], "<recorded swarm parser>", "exec"), namespace)
    return namespace["build_parser"]


def _parsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    found = {path: parser}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                found.update(_parsers(child, (*path, name)))
    return found


def _callable_name(value: Any) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _refusal(parser: argparse.ArgumentParser, argv: list[str]) -> dict[str, Any]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            parser.parse_args(argv)
        except SystemExit as error:
            code = error.code
    return {"argv": argv, "code": code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}


def _snapshot(parser: argparse.ArgumentParser, records: dict[str, Any]) -> bytes:
    result = {
        "namespaces": [{"argv": argv, "namespace": vars(parser.parse_args(argv))} for argv in records["arguments"]],
        "help": [{"path": path, "text": child.format_help()} for path, child in _parsers(parser).items()],
        "refusals": [_refusal(parser, argv) for argv in records["rejected"]],
    }
    return (json.dumps(result, default=_callable_name, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.mark.feature("swarm-parser-extraction")
def test_recorded_arguments_cover_every_parser_leaf(records: dict[str, Any]) -> None:
    parser = _reference(records)()
    leaves = {
        path
        for path, child in _parsers(parser).items()
        if not any(isinstance(action, argparse._SubParsersAction) for action in child._actions)
    }
    covered = {path for path in leaves if any(tuple(argv[1 : len(path) + 1]) == path for argv in records["arguments"])}
    assert covered == leaves
    for argv in records["arguments"]:
        parser.parse_args(argv)


@pytest.mark.feature("swarm-parser-extraction")
def test_namespaces_help_and_refusals_equal_recorded_base(records: dict[str, Any]) -> None:
    base_parser, head_parser = _reference(records)(), cli.build_parser()
    base, head = _snapshot(base_parser, records), _snapshot(head_parser, records)
    if report_dir := os.environ.get("PROOF_REPORT_DIR"):
        source = subprocess.run(
            ["git", "show", f"{records['base']}:scripts/swarm/cli.py"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        builder = next(
            node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "build_parser"
        )
        assert ast.get_source_segment(source, builder) == records["builder"].rstrip()
        reports = Path(report_dir)
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "swarm-parser-base.json").write_bytes(base)
        (reports / "swarm-parser-head.json").write_bytes(head)
        warnings.warn(
            f"Swarm parser equivalence: {len(records['arguments'])} vectors, {len(_parsers(base_parser))} parsers, "
            f"base={hashlib.sha256(base).hexdigest()}, head={hashlib.sha256(head).hexdigest()}",
            stacklevel=1,
        )
    assert head == base
    for argv in records["arguments"]:
        assert vars(head_parser.parse_args(argv)) == vars(base_parser.parse_args(argv))


@pytest.mark.feature("swarm-parser-extraction")
def test_recorded_invalid_arguments_are_refused(records: dict[str, Any]) -> None:
    parser = cli.build_parser()
    assert all(_refusal(parser, argv)["code"] == 2 for argv in records["rejected"])


@pytest.mark.feature("swarm-parser-extraction")
@pytest.mark.parametrize(
    "command,handler",
    [("probe", "probe_command"), ("tick", "cmd_swarm_tick"), ("blockers", "cmd_blockers"), ("panic", "cmd_panic")],
)
def test_builder_resolves_current_cli_handlers(monkeypatch: pytest.MonkeyPatch, command: str, handler: str) -> None:
    def replacement(args: argparse.Namespace) -> None:
        pass

    monkeypatch.setattr(cli, handler, replacement)
    assert cli.build_parser().parse_args(["sample", command]).handler is replacement


def _tracked_cli(root: Path) -> Path:
    """Return the swarm cli source under ``root``, or under its parent when ``root`` is the mutants copy that a
    mutation run imports with every mutated function body duplicated."""
    return (root.parent if root.name == "mutants" else root) / "scripts" / "swarm" / "cli.py"


@pytest.mark.feature("swarm-parser-extraction")
def test_parser_is_separate_and_cli_has_size_headroom() -> None:
    assert cli.build_parser.__module__ == "scripts.swarm.cli_parser"
    assert len(_tracked_cli(ROOT).read_text().splitlines()) < 2850


@pytest.mark.feature("swarm-parser-extraction")
def test_headroom_counts_the_tracked_cli_beside_a_mutants_copy(tmp_path: Path) -> None:
    for folder, lines in ((tmp_path, 2823), (tmp_path / "mutants", 3646)):
        (folder / "scripts" / "swarm").mkdir(parents=True)
        (folder / "scripts" / "swarm" / "cli.py").write_text("pass\n" * lines)
    assert _tracked_cli(tmp_path / "mutants") == tmp_path / "scripts" / "swarm" / "cli.py"
    assert len(_tracked_cli(tmp_path / "mutants").read_text().splitlines()) == 2823
    assert _tracked_cli(tmp_path) == tmp_path / "scripts" / "swarm" / "cli.py"
