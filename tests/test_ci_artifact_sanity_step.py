"""The lint job's artifact sanity step runs the protected grader copy of the checker on the head page folder."""

import ast
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest
import yaml
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
CHECKER = Path("scripts") / "swarm_ledger" / "artifact_sanity.py"


NO_PAGES = (
    "::error::The protected grader carries no artifact sanity checker with the pages option, "
    "so nothing trusted can grade the head viewer page and assets.\n"
)


def _lint_steps() -> list[dict[str, str]]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]["lint"]["steps"]


def _named(steps: list[dict[str, str]], name: str) -> dict[str, str]:
    return next(step for step in steps if step.get("name") == name)


def _checker(name: str, option: str, code: int) -> str:
    return (
        "import os, sys\n"
        f"OPTION = {option}\n"
        "with open(os.environ['CALLS'], 'a') as calls:\n"
        f"    calls.write(' '.join(['{name}', os.getcwd(), *sys.argv[1:]]) + '\\n')\n"
        f"sys.exit({code})\n"
    )


def _run_step(tmp_path: Path, grader_option: str | None) -> tuple[int, str]:
    grader, head, tools = tmp_path / "temp" / "grader", tmp_path / "head", tmp_path / "bin"
    grader.mkdir(parents=True)
    checkers = [(head, _checker("planted", '"--pages"', 0))]
    if grader_option is not None:
        checkers.append((grader, _checker("grader", grader_option, 1)))
    for root, source in checkers:
        (root / CHECKER).parent.mkdir(parents=True)
        (root / CHECKER).write_text(source)
        (root / "scripts" / "__init__.py").write_text("")
        (root / CHECKER.parent / "__init__.py").write_text("")
    fixtures = head / "tests" / "fixtures" / "artifacts"
    fixtures.mkdir(parents=True)
    (fixtures / "notes.md").write_text("# Notes\n")
    tools.mkdir()
    (tools / "python").symlink_to(sys.executable)
    env = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "temp"),
        "GITHUB_WORKSPACE": str(head),
        "CALLS": str(tmp_path / "calls"),
    }
    script = _named(_lint_steps(), "Artifact sanity checks")["run"]
    command = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
    done = subprocess.run(command, cwd=head, env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, (calls.read_text() if calls.exists() else "") + done.stdout


def _module_file(name: str) -> Path | None:
    base = ROOT.joinpath(*name.split("."))
    return next((path for path in (base.with_suffix(".py"), base / "__init__.py") if path.is_file()), None)


def _import_time(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(child, ast.If) and ast.unparse(child.test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING"):
            yield from (item for branch in child.orelse for item in (branch, *_import_time(branch)))
            continue
        yield child
        yield from _import_time(child)


def _imports(name: str, path: Path, everywhere: bool) -> Iterator[str]:
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree) if everywhere else _import_time(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            anchor = parts[: len(parts) + 1 - node.level] if node.level else []
            base = ".".join([*anchor, node.module] if node.module else anchor)
            targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        for target in targets:
            top = target.partition(".")[0]
            yield f"{package}.{target}" if _module_file(top) is None and _module_file(f"{package}.{top}") else target


def _checker_packages() -> set[str]:
    seen: set[str] = set()
    found: set[str] = set()
    pending = [(".".join(CHECKER.with_suffix("").parts), True)]
    while pending:
        name, everywhere = pending.pop()
        path = _module_file(name)
        if name in seen or path is None:
            continue
        seen.add(name)
        for target in _imports(name, path, everywhere):
            parts = target.split(".")
            if _module_file(parts[0]) is None and parts[0] not in sys.stdlib_module_names:
                found.add(parts[0])
            pending += [(".".join(parts[:end]), False) for end in range(1, len(parts) + 1)]
    return found


def _interpreter_installs(run: str) -> set[str]:
    names: set[str] = set()
    for line in run.replace("\\\n", " ").splitlines():
        if not line.lstrip().startswith("python -m pip install ") or any(mark in line for mark in "&;|"):
            continue
        words = iter(shlex.split(line)[4:])
        for word in words:
            if word in ("--constraint", "-c"):
                next(words, None)
            elif not word.startswith("-"):
                names.add(canonicalize_name(re.split(r"[^A-Za-z0-9._-]", word, maxsplit=1)[0]))
    return names


def test_the_grader_is_checked_out_before_the_artifact_checks() -> None:
    steps = _lint_steps()
    grader = _named(steps, "Check out the protected grader")
    assert steps.index(grader) < steps.index(_named(steps, "Artifact sanity checks"))


def test_a_planted_head_checker_never_runs_once_the_grader_carries_the_pages_option(tmp_path: Path) -> None:
    head = tmp_path / "head"
    pages, notes = head / "scripts" / "swarm_ledger", head / "tests" / "fixtures" / "artifacts" / "notes.md"
    assert _run_step(tmp_path, '"--pages"') == (1, f"grader {tmp_path / 'temp' / 'grader'} --pages {pages} {notes}\n")


def test_a_grader_without_the_pages_option_turns_the_step_red(tmp_path: Path) -> None:
    assert _run_step(tmp_path, '"files"') == (1, NO_PAGES)


def test_a_grader_without_the_checker_turns_the_step_red(tmp_path: Path) -> None:
    assert _run_step(tmp_path, None) == (1, NO_PAGES)


def test_the_step_detects_the_option_the_checker_really_declares() -> None:
    assert '"--pages"' in (ROOT / CHECKER).read_text()


@pytest.mark.feature("ci-artifact-sanity-interpreter-imports")
def test_the_lint_interpreter_installs_every_package_the_artifact_checker_imports() -> None:
    steps = _lint_steps()
    before = steps[: steps.index(_named(steps, "Artifact sanity checks"))]
    runs = [step.get("run", "") for step in before if not step.get("continue-on-error")]
    installed = {name for run in runs for name in _interpreter_installs(run)}
    known = packages_distributions()
    missing = [
        module
        for module in sorted(_checker_packages())
        if not {canonicalize_name(dist) for dist in known.get(module, [module])} & installed
    ]
    assert missing == []
