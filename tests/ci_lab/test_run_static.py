import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_wiring
from scripts.ci_lab import run_static, static_gates
from scripts.ci_lab.sandbox import PortAllocator
from scripts.ci_lab.static_gates import StaticGate
from scripts.dep_manifest import _git

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = "tests/SIZE_ALLOWLIST.json"
NARROW = "def narrow(a: int) -> int:\n    return a\n"
WIDE = "def wide(a, b, c, d, e, f, g, h):\n    return a\n"
PASSING = (
    "def main(argv: object = None) -> int:\n    return 0\n\n\n"
    "if __name__ == '__main__':\n    raise SystemExit(main())\n"
)
LAYOUT = (
    'pwd -P\necho "$GITHUB_WORKSPACE"\ncd "$RUNNER_TEMP/grader" && pwd -P\ncd "$RUNNER_TEMP/base" && pwd -P\n'
    'command -v python\necho "$PATH"\necho "$CI_LAB_PORT_FIRST"\necho "$CI_LAB_TEST"\n'
    'echo summary > "$GITHUB_STEP_SUMMARY"\n'
)
CHANGED = "${{ !cancelled() && steps.self-plant.outputs.changed == 'true' }}"
ECHOES = 'echo "$BASE"\necho "$RULES"\necho "$CI_LAB_TEST"\necho "$GITHUB_WORKSPACE"\necho "$RUNNER_TEMP"\n'
WRITES = (
    "for tree in grader base; do\n"
    '  if { echo planted > "$RUNNER_TEMP/$tree/planted.txt"; } 2>/dev/null; then echo "$tree written"\n'
    '  else echo "$tree refused"; fi\n'
    "done\n"
    'echo unit > "$GITHUB_WORKSPACE/unit.txt"\n'
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


def _step(run: str, env: tuple[tuple[str, str], ...] = (), condition: str = "") -> StaticGate:
    return StaticGate("lint/probe", "lint", "Probe", ("python",), (), run, (".",), (".",), env, condition)


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


@pytest.mark.feature("lab-static-step-tools")
def test_the_step_reads_each_tree_where_the_workflow_puts_it(tmp_path: Path, settings: run_static.Settings) -> None:
    head, base, grader = _folders(tmp_path)
    result = run_static.run(_step(LAYOUT), head, base, grader, settings)
    assert result.gate == "lint/probe"
    assert result.green
    trees = [str(head.resolve()), str(head), str(grader.resolve()), str(base.resolve())]
    bin_folder = settings.venv / "bin"
    path = os.pathsep.join((str(bin_folder), "/usr/local/bin", os.defpath))
    assert result.log.read_text().splitlines() == [*trees, str(bin_folder / "python"), path, "41000", "kept"]
    assert result.report.read_text() == "summary\n"
    assert settings.root in result.report.parents
    assert settings.root in result.log.parents


@pytest.mark.feature("lab-static-step-tools")
def test_a_step_keeps_the_ruff_cache_in_its_run_folder_outside_every_tree(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    head, base, grader = _folders(tmp_path)
    result = run_static.run(_step('echo "$RUFF_CACHE_DIR"\necho "$RUNNER_TEMP"\n'), head, base, grader, settings)
    cache, folder = (Path(line) for line in result.log.read_text().splitlines())
    assert cache == folder / "ruff"
    assert settings.root in cache.parents
    assert not {head, base, grader} & set(cache.parents)
    named = _step('echo "$RUFF_CACHE_DIR"\n', (("RUFF_CACHE_DIR", "/elsewhere"),))
    assert run_static.run(named, head, base, grader, settings).log.read_text() == "/elsewhere\n"


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_a_step_runs_one_type_checker_at_a_time_unless_its_env_names_another_count(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    head, base, grader = _folders(tmp_path)
    result = run_static.run(_step('echo "$AGENTIHOOKS_TYPE_WORKERS"\n'), head, base, grader, settings)
    assert result.log.read_text() == "1\n"
    named = _step('echo "$AGENTIHOOKS_TYPE_WORKERS"\n', (("AGENTIHOOKS_TYPE_WORKERS", "3"),))
    assert run_static.run(named, head, base, grader, settings).log.read_text() == "3\n"


@pytest.mark.feature("lab-static-step-tools")
def test_ruff_passes_on_a_head_tree_that_refuses_writes(tmp_path: Path, settings: run_static.Settings) -> None:
    head, base, grader = _folders(tmp_path)
    (head / "sample.py").write_text("VALUE = 1\n")
    head.chmod(0o555)
    try:
        assert os.geteuid() == 0 or not os.access(head, os.W_OK)
        result = run_static.run(_step("python -m ruff check --isolated .\n"), head, base, grader, settings)
        assert result.green, result.log.read_text()
        assert not (head / ".ruff_cache").exists()
    finally:
        head.chmod(0o755)


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


@pytest.mark.feature("lab-static-read-only-trees")
def test_the_step_runs_with_the_grader_and_base_trees_bound_read_only(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    head, base, grader = _folders(tmp_path)
    assert run_static.run(_step("true\n"), head, base, grader, settings).green
    args = (tmp_path / "bwrap.args").read_text().splitlines()
    binds = ["--ro-bind", str(grader), str(grader), "--ro-bind", str(base), str(base)]
    assert args[: args.index("--")] == ["--dev-bind", "/", "/", *binds, "--unshare-net", "--die-with-parent"]


def test_a_step_cannot_write_into_the_grader_or_base_tree(
    tmp_path: Path, settings: run_static.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders = os.environ["PATH"].split(os.pathsep)
    monkeypatch.setenv("PATH", os.pathsep.join(folder for folder in folders if folder != str(tmp_path / "bin")))
    bwrap = shutil.which("bwrap")
    probe = [str(bwrap), "--dev-bind", "/", "/", "--unshare-net", "true"]
    if bwrap is None or subprocess.run(probe, capture_output=True, check=False).returncode != 0:
        pytest.skip("bubblewrap cannot create namespaces on this runner")
    head, base, grader = _folders(tmp_path)
    result = run_static.run(_step(WRITES), head, base, grader, settings)
    assert (result.code, result.log.read_text().splitlines()) == (0, ["grader refused", "base refused"])
    assert [*grader.iterdir(), *base.iterdir()] == []
    assert (head / "unit.txt").read_text() == "unit\n"


@pytest.mark.feature("lab-static-step-env")
def test_a_step_whose_if_is_false_is_skipped_green_without_running(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    marker = tmp_path / "ran"
    gate = _step(f"touch '{marker}'\n", condition=CHANGED)
    trees = _folders(tmp_path)
    unchanged = dataclasses.replace(settings, context={"steps.self-plant.outputs.changed": "false"})
    result = run_static.run(gate, *trees, unchanged)
    assert (result.gate, result.code, result.skipped, result.green) == ("lint/probe", None, True, True)
    assert result.log.read_text() == f"if is false: {CHANGED}\n"
    assert result.report.read_text() == ""
    assert result.log == result.report.parent / "skipped.txt"
    assert settings.root in result.log.parents
    assert not marker.exists()
    assert not (tmp_path / "bwrap.args").exists()
    held = dataclasses.replace(settings, context={"steps.self-plant.outputs.changed": "true"})
    ran = run_static.run(gate, *trees, held)
    assert (ran.code, ran.skipped, ran.green) == (0, False, True)
    assert marker.exists()


@pytest.mark.feature("lab-static-step-env")
def test_the_step_reads_its_env_with_expressions_resolved_from_the_context(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    env = (
        ("BASE", "${{ inputs.base }}"),
        ("RULES", ".semgrep"),
        ("CI_LAB_TEST", "step"),
        ("RUNNER_TEMP", "/nowhere"),
        ("GITHUB_WORKSPACE", "/nowhere"),
    )
    gate = _step(ECHOES, env, "${{ !cancelled() }}")
    head, base, grader = _folders(tmp_path)
    context = dataclasses.replace(settings, context={"inputs.base": "origin/dev"})
    result = run_static.run(gate, head, base, grader, context)
    assert (result.code, result.skipped) == (0, False)
    lines = result.log.read_text().splitlines()
    assert lines[:4] == ["origin/dev", ".semgrep", "step", str(head)]
    assert settings.root in Path(lines[4]).parents


MATRIX = r"""
jobs:
  lint:
    strategy:
      matrix:
        python-version: ["3.12"]
    steps:
      - name: Read the run
        if: ${{ github.event_name == 'pull_request' }}
        env:
          COUNTS: ${{ runner.temp }}/counts
          PYTHON: ${{ matrix.python-version }}
        run: python -m scripts.read_the_run
"""


@pytest.mark.feature("lab-static-test-count-units")
def test_the_step_reads_runner_temp_as_its_run_folder_and_each_matrix_value_its_job_holds(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    (gate,) = static_gates.discover(MATRIX)
    probe = dataclasses.replace(gate, run='echo "$COUNTS"\necho "$RUNNER_TEMP/counts"\necho "$PYTHON"\n')
    head, base, grader = _folders(tmp_path)
    context = dataclasses.replace(settings, context={"github.event_name": "pull_request"})
    result = run_static.run(probe, head, base, grader, context)
    assert (result.code, result.skipped) == (0, False)
    counts, temp, python = result.log.read_text().splitlines()
    assert counts == temp
    assert settings.root in Path(counts).parents
    assert python == "3.12"


PRODUCING = r"""
jobs:
  lint:
    steps:
      - name: Mint a token
        id: token
        uses: actions/create-github-app-token@v3
      - name: Fetch with the token
        id: fetch
        if: ${{ steps.token.outcome == 'success' }}
        run: touch "$RUNNER_TEMP/fetched"
      - name: Pick the pages
        id: pick
        env:
          CHOICE: ${{ inputs.base }}
        run: |
          echo picking
          printf 'browser=%s\n\nnote=a=b\n' "$CHOICE" >> "$GITHUB_OUTPUT"
      - name: Check the pages
        if: ${{ !cancelled() && steps.pick.outputs.browser == 'true' }}
        env:
          SEEN: ${{ steps.pick.outputs.browser }} ${{ steps.pick.outcome }} ${{ steps.pick.conclusion }} ${{ steps.pick.outputs.note }}
          UNSET: x${{ steps.pick.outputs.unset }}x
          TOKEN: ${{ steps.token.outcome }} ${{ steps.token.conclusion }} x${{ steps.token.outputs.token }}x ${{ steps.fetch.outcome }}
        run: python -m scripts.check_pages
"""

FAILING = r"""
jobs:
  lint:
    steps:
      - name: Break
        id: broken
        run: |
          echo "half=1" >> "$GITHUB_OUTPUT"
          echo "the earlier step failed"
          exit 3
      - name: Check
        env:
          HALF: ${{ steps.broken.outputs.half }}
        run: python -m scripts.check
"""

WRITTEN = r"""
jobs:
  lint:
    steps:
      - name: Write the lines
        id: block
        run: printf 'LINES' >> "$GITHUB_OUTPUT"
      - name: Check
        env:
          TEXT: ${{ steps.block.outputs.text }}
        run: python -m scripts.check
"""

SLOW = r"""
jobs:
  lint:
    steps:
      - name: Hang
        id: slow
        run: sleep 30
      - name: Check
        env:
          DONE: ${{ steps.slow.outputs.done }}
        run: python -m scripts.check
"""


@pytest.mark.feature("lab-static-step-env")
def test_earlier_steps_run_first_and_their_outputs_outcome_and_conclusion_reach_the_gate(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    (gate,) = static_gates.discover(PRODUCING)
    probe = dataclasses.replace(gate, run='echo "$SEEN"\necho "$UNSET"\necho "$TOKEN"\n')
    head, base, grader = _folders(tmp_path)
    graded = run_static.run(probe, head, base, grader, dataclasses.replace(settings, context={"inputs.base": "true"}))
    assert (graded.code, graded.skipped) == (0, False)
    assert graded.log.read_text().splitlines() == ["true success success a=b", "xx", "skipped skipped xx skipped"]
    assert not list(settings.root.rglob("fetched"))
    unpicked = dataclasses.replace(settings, context={"inputs.base": "false"})
    skipped = run_static.run(probe, head, base, grader, unpicked)
    assert (skipped.code, skipped.skipped, skipped.green) == (None, True, True)


@pytest.mark.feature("lab-static-step-env")
def test_a_failing_earlier_step_fails_the_gate_without_running_it(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    (gate,) = static_gates.discover(FAILING)
    head, base, grader = _folders(tmp_path)
    graded = run_static.run(
        dataclasses.replace(gate, run='touch "$GITHUB_WORKSPACE/ran"'), head, base, grader, settings
    )
    assert (graded.gate, graded.code, graded.skipped, graded.green) == ("lint/check", 3, False, False)
    assert graded.log.read_text() == "the earlier step failed\n"
    assert graded.report.name == run_static.SUMMARY
    assert not (head / "ran").exists()


@pytest.mark.feature("lab-static-step-env")
def test_an_earlier_step_past_its_time_limit_ends_the_gate_red_with_no_code(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    (gate,) = static_gates.discover(SLOW)
    head, base, grader = _folders(tmp_path)
    probe = dataclasses.replace(gate, run='touch "$GITHUB_WORKSPACE/ran"')
    graded = run_static.run(probe, head, base, grader, dataclasses.replace(settings, timeout=0.5))
    assert (graded.code, graded.skipped, graded.green) == (None, False, False)
    assert not (head / "ran").exists()


@pytest.mark.feature("lab-static-step-env")
@pytest.mark.parametrize(
    "lines",
    [r"text<<EOF\nhidden line\nEOF\n", r"hidden line\n", r"text<<EOF=\nnote=held\nEOF=\n"],
    ids=["delimited", "bare", "delimiter-with-equals"],
)
def test_an_earlier_step_output_line_outside_name_equals_value_is_refused_without_its_text(
    tmp_path: Path, settings: run_static.Settings, lines: str
) -> None:
    (gate,) = static_gates.discover(WRITTEN.replace("LINES", lines))
    with pytest.raises(ValueError) as refused:
        run_static.run(gate, *_folders(tmp_path), settings)
    assert str(refused.value) == "block wrote an output line that is not name=value"


def test_a_condition_outside_the_expression_subset_is_refused_before_the_step_runs(
    tmp_path: Path, settings: run_static.Settings
) -> None:
    gate = _step("true\n", condition="${{ contains(github.ref, 'dev') }}")
    with pytest.raises(ValueError) as refused:
        run_static.run(gate, *_folders(tmp_path), settings)
    assert str(refused.value) == "cannot read ','"
    assert not (tmp_path / "bwrap.args").exists()
