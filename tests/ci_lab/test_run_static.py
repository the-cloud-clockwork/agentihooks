import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

from scripts import ci_wiring
from scripts.ci_lab import run_static, static_gates
from scripts.ci_lab.sandbox import PortAllocator
from scripts.ci_lab.static_gates import StaticGate
from tests.dep_manifest import _git

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = "tests/SIZE_ALLOWLIST.json"
NARROW = "def narrow(a: int) -> int:\n    return a\n"
WIDE = "def wide(a, b, c, d, e, f, g, h):\n    return a\n"
PASSING = (
    "def main(argv: object = None) -> int:\n    return 0\n\n\n"
    "if __name__ == '__main__':\n    raise SystemExit(main())\n"
)
LAYOUT = (
    'pwd -P\ncd "$RUNNER_TEMP/grader" && pwd -P\ncd "$RUNNER_TEMP/base" && pwd -P\n'
    'command -v python\necho "$PATH"\necho "$CI_LAB_PORT_FIRST"\necho "$CI_LAB_TEST"\n'
    'echo summary > "$GITHUB_STEP_SUMMARY"\n'
)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> run_static.Settings:
    folder = tmp_path / "bin"
    folder.mkdir()
    bwrap = folder / "bwrap"
    bwrap.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{tmp_path / 'bwrap.args'}'\n"
        'while [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'
    )
    bwrap.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    root = tmp_path / "runs"
    root.mkdir()
    return run_static.Settings(
        root=root,
        venv=Path(sys.executable).parent.parent,
        ports=PortAllocator(41000, 41099, 10),
        whitelist={"CI_LAB_TEST": "kept", "GITHUB_WORKSPACE": "/nowhere", "RUNNER_TEMP": "/nowhere"},
        timeout=120.0,
    )


def _tree(path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "tree")
    return path


def _size_gate() -> StaticGate:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    return next(gate for gate in gates if gate.modules == ("scripts.size_limits",))


def _grader(tmp_path: Path) -> Path:
    real = (ROOT / "scripts" / "size_limits.py").read_text()
    return _tree(tmp_path / "grader", {"scripts/__init__.py": "", "scripts/size_limits.py": real})


def _base(tmp_path: Path) -> Path:
    return _tree(tmp_path / "base", {"narrow.py": NARROW, ALLOWLIST: "{}\n"})


def _step(run: str) -> StaticGate:
    return StaticGate("lint/probe", "lint", "Probe", ("python",), (), run, (".",), (".",))


def _folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    trees = tuple(tmp_path / name for name in ("head", "base", "grader"))
    for tree in trees:
        tree.mkdir()
    return trees[0], trees[1], trees[2]


def test_a_head_that_edits_the_size_gate_to_pass_stays_red_under_the_grader_copy(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    files = {"narrow.py": NARROW, "wide.py": WIDE, ALLOWLIST: "{}\n", "scripts/__init__.py": ""}
    head = _tree(tmp_path / "head", {**files, "scripts/size_limits.py": PASSING})
    base = _base(tmp_path)
    graded = run_static.run(_size_gate(), head, base, _grader(tmp_path), settings)
    assert graded.code == 1
    assert not graded.green
    assert "wide.py::wide breaks PLR0913 at 8; it is not on the base allowlist" in graded.log.read_text()
    assert run_static.run(_size_gate(), head, base, head, settings).green


def test_a_clean_head_is_green_under_the_grader_copy(tmp_path: Path, settings: run_static.Settings) -> None:
    head = _tree(tmp_path / "head", {"narrow.py": NARROW, ALLOWLIST: "{}\n"})
    graded = run_static.run(_size_gate(), head, _base(tmp_path), _grader(tmp_path), settings)
    assert graded.code == 0
    assert graded.green
    assert "0 units over a limit, 0 on the base allowlist, 0 errors" in graded.log.read_text()


def test_the_step_reads_each_tree_where_the_workflow_puts_it(tmp_path: Path, settings: run_static.Settings) -> None:
    head, base, grader = _folders(tmp_path)
    result = run_static.run(_step(LAYOUT), head, base, grader, settings)
    assert result.gate == "lint/probe"
    assert result.green
    trees = [str(head.resolve()), str(grader.resolve()), str(base.resolve())]
    bin_folder = settings.venv / "bin"
    path = f"{bin_folder}{os.pathsep}{os.defpath}"
    assert result.log.read_text().splitlines() == [*trees, str(bin_folder / "python"), path, "41000", "kept"]
    assert result.report.read_text() == "summary\n"
    assert settings.root in result.report.parents
    assert settings.root in result.log.parents


@pytest.mark.parametrize("step", ["false\necho reached\n", "false | true\necho reached\n"])
def test_a_failing_command_ends_the_step_red(tmp_path: Path, settings: run_static.Settings, step: str) -> None:
    result = run_static.run(_step(step), *_folders(tmp_path), settings)
    assert result.code == 1
    assert not result.green
    assert "reached" not in result.log.read_text()


def test_a_step_past_its_time_limit_is_red_with_no_code(tmp_path: Path, settings: run_static.Settings) -> None:
    result = run_static.run(_step("sleep 30\n"), *_folders(tmp_path), dataclasses.replace(settings, timeout=0.5))
    assert result.code is None
    assert not result.green
    assert result.report.read_text() == ""


def test_the_step_runs_inside_bubblewrap_without_network(tmp_path: Path, settings: run_static.Settings) -> None:
    assert run_static.run(_step("true\n"), *_folders(tmp_path), settings).green
    assert "--unshare-net" in (tmp_path / "bwrap.args").read_text().splitlines()
