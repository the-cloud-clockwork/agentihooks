"""The static gates the released lab reads from a head are held to the gates the head's own code reads.

A reading runs the discovery command line of one code folder over one tree. The code folders here are stubs whose
command prints the gates a tree lists less the names that code ignores, so two codes read one tree differently the
way a released scheduler and a head did after the job split.
"""

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_wiring
from scripts.ci_lab import discovery_parity, static_exclusions, static_gates

ROOT = Path(__file__).resolve().parents[2]
STEP = "Hold the static gates the released lab reads equal to the gates the head reads"
REAL = frozenset({"test-count/hold-the-base-revision-s-test-count", "type-gate/hold-the-strict-type-checker"})
INSTALLS = frozenset({"test-count/install-dependencies", "type-gate/install-dependencies"})
ONLY_RELEASED = "only the released code grades: test-count/install-dependencies, type-gate/install-dependencies"
COMMAND = """import json
import sys
from pathlib import Path

IGNORED = {ignored!r}
root = Path(sys.argv[sys.argv.index("--root") + 1])
for name, missing in json.loads((root / "gates.json").read_text()).items():
    if name not in IGNORED:
        print(json.dumps({{"name": name, "missing": missing}}))
"""


def _code(folder: Path, ignored: Iterable[str] = (), fails: bool = False) -> Path:
    package = folder / "scripts" / "ci_lab"
    package.mkdir(parents=True)
    (folder / "scripts" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    body = "raise SystemExit(3)\n" if fails else COMMAND.format(ignored=sorted(ignored))
    (package / "static_gates.py").write_text(body)
    return folder


def _tree(folder: Path, gates: Mapping[str, list[str]], excluded: Iterable[str] = ()) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gates.json").write_text(json.dumps(gates))
    catalog = {name: {"reason": "the lab cannot run it"} for name in excluded}
    if catalog:
        path = folder / static_exclusions.PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(catalog))
    return folder


def _gates(*names: Iterable[str]) -> dict[str, list[str]]:
    return {name: [] for group in names for name in sorted(group)}


def _run(released: Path, base: Path, head: Path) -> int:
    return discovery_parity.main(["--released", str(released), "--base", str(base), "--head", str(head)])


@pytest.mark.feature("lab-discovery-parity-with-release")
@pytest.mark.parametrize(
    ("released", "own", "before"),
    [
        (REAL, REAL, REAL),
        (REAL | INSTALLS, REAL | INSTALLS, REAL),
        (REAL | INSTALLS, REAL, REAL | INSTALLS),
        (None, REAL, None),
    ],
    ids=["every reading agrees", "both codes read a changed tree alike", "the released reading is unchanged", "fails"],
)
def test_a_head_the_released_code_reads_as_its_own_code_does_or_as_it_reads_the_base_is_not_refused(
    released: frozenset[str] | None, own: frozenset[str] | None, before: frozenset[str] | None
) -> None:
    assert discovery_parity.problems(released, own, before) == []


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_head_the_released_code_reads_unlike_its_own_code_and_unlike_the_base_is_refused_by_gate_name() -> None:
    assert discovery_parity.problems(REAL | INSTALLS, REAL | {"lint/new"}, REAL) == [
        discovery_parity.DIFFERS,
        ONLY_RELEASED,
        "only the head's code grades: lint/new",
        discovery_parity.REMEDY,
    ]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_side_that_grades_no_gate_of_its_own_is_named_as_none() -> None:
    found = discovery_parity.problems(REAL, REAL | {"lint/new"}, REAL | INSTALLS)
    assert found[1:3] == ["only the released code grades: none", "only the head's code grades: lint/new"]


@pytest.mark.feature("lab-discovery-parity-with-release")
@pytest.mark.parametrize("before", [REAL, REAL | INSTALLS], ids=["the base is read", "the base reads otherwise"])
def test_a_head_the_released_code_cannot_read_while_it_reads_the_base_is_refused(before: frozenset[str]) -> None:
    assert discovery_parity.problems(None, REAL, before) == [discovery_parity.BROKEN, discovery_parity.REMEDY]


@pytest.mark.feature("lab-discovery-parity-with-release")
@pytest.mark.parametrize("released", [None, REAL], ids=["no code reads it", "the released code reads it"])
def test_a_head_its_own_code_cannot_read_is_refused(released: frozenset[str] | None) -> None:
    assert discovery_parity.problems(released, None, released) == [discovery_parity.UNREADABLE]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_reading_runs_the_command_of_the_code_it_is_given_and_drops_a_gate_that_misses_a_name(
    tmp_path: Path,
) -> None:
    tree = _tree(tmp_path / "tree", {"lint/a": [], "lint/b": [], "lint/c": ["github.head_ref"]})
    assert discovery_parity.graded(_code(tmp_path / "old"), tree) == {"lint/a", "lint/b"}
    assert discovery_parity.graded(_code(tmp_path / "new", ignored=["lint/b"]), tree) == {"lint/a"}


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_reading_runs_the_code_it_is_given_when_python_leaves_the_working_folder_off_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    tree = _tree(tmp_path / "tree", {"lint/a": [], "lint/b": []})
    assert discovery_parity.graded(_code(tmp_path / "new", ignored=["lint/b"]), tree) == {"lint/a"}


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_reading_leaves_out_the_gates_the_exclusion_catalog_of_its_tree_names(tmp_path: Path) -> None:
    code = _code(tmp_path / "code")
    gates = _gates(REAL, INSTALLS)
    assert discovery_parity.graded(code, _tree(tmp_path / "with", gates, excluded=INSTALLS)) == REAL
    assert discovery_parity.graded(code, _tree(tmp_path / "without", gates)) == REAL | INSTALLS


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_a_command_that_fails_gives_a_failed_reading(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "tree", _gates(REAL))
    assert discovery_parity.graded(_code(tmp_path / "code", fails=True), tree) is None


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_of_this_checkout_reads_this_checkout_to_the_gates_its_discovery_returns() -> None:
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    found = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    read = discovery_parity.graded(ROOT, ROOT)
    assert read
    assert read == {gate.name for gate in found if not gate.missing}


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_refuses_a_head_that_drops_the_catalog_while_the_released_code_still_reads_the_install_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = _gates(REAL, INSTALLS)
    released = _code(tmp_path / "released")
    base = _tree(tmp_path / "base", gates, excluded=INSTALLS)
    head = _code(_tree(tmp_path / "head", gates), ignored=INSTALLS)
    assert _run(released, base, head) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"::error::{discovery_parity.DIFFERS}",
        f"::error::{ONLY_RELEASED}",
        "::error::only the head's code grades: none",
        f"::error::{discovery_parity.REMEDY}",
    ]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_passes_a_head_that_changes_only_how_gates_are_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = _gates(REAL, INSTALLS)
    released = _code(tmp_path / "released")
    base = _tree(tmp_path / "base", gates, excluded=INSTALLS)
    head = _code(_tree(tmp_path / "head", gates, excluded=INSTALLS), ignored=INSTALLS)
    assert _run(released, base, head) == 0
    assert capsys.readouterr().out.splitlines() == ["Discovery parity: the released code and the head read 2 gates"]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_tells_when_the_codes_disagree_on_a_head_that_left_the_released_reading_as_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = _gates(REAL, INSTALLS)
    released = _code(tmp_path / "released")
    base = _tree(tmp_path / "base", gates)
    head = _code(_tree(tmp_path / "head", gates), ignored=INSTALLS)
    assert _run(released, base, head) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"::notice::{discovery_parity.LAGS}",
        f"::notice::{ONLY_RELEASED}",
        "::notice::only the head's code grades: none",
    ]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_refuses_a_head_whose_own_code_cannot_read_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = _gates(REAL)
    released = _code(tmp_path / "released")
    head = _code(_tree(tmp_path / "head", gates), fails=True)
    assert _run(released, _tree(tmp_path / "base", gates), head) == 1
    assert capsys.readouterr().out.splitlines() == [f"::error::{discovery_parity.UNREADABLE}"]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_command_tells_when_the_released_code_reads_neither_the_head_nor_the_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = _gates(REAL)
    released = _code(tmp_path / "released", fails=True)
    head = _code(_tree(tmp_path / "head", gates))
    assert _run(released, _tree(tmp_path / "base", gates), head) == 0
    assert capsys.readouterr().out.splitlines() == [f"::notice::{discovery_parity.STUCK}"]


def _lint_actions() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    return workflow["jobs"]["lint-actions"]["steps"]


@pytest.mark.feature("lab-discovery-parity-with-release")
def test_the_lint_actions_job_reads_the_head_with_the_scripts_of_main_after_the_base_and_grader_checkouts() -> None:
    names = [step.get("name") for step in _lint_actions()]
    assert names.count(STEP) == 1
    assert names.index("Check out the base revision") < names.index(STEP)
    assert names.index("Check out the protected grader") < names.index(STEP)
    step = _lint_actions()[names.index(STEP)]
    assert set(step) == {"name", "if", "run"}
    assert step["if"] == "${{ !cancelled() }}"
    assert step["run"].splitlines() == [
        "git fetch --no-tags origin main",
        'mkdir "$RUNNER_TEMP/released"',
        'git archive FETCH_HEAD scripts | tar -x -C "$RUNNER_TEMP/released"',
        'if [[ -f "$RUNNER_TEMP/grader/scripts/ci_lab/discovery_parity.py" ]]; then',
        '  cd "$RUNNER_TEMP/grader"',
        "fi",
        'python -m scripts.ci_lab.discovery_parity --released "$RUNNER_TEMP/released" --base "$RUNNER_TEMP/base" '
        '--head "$GITHUB_WORKSPACE"',
    ]
