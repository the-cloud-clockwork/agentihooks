import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_wiring
from scripts.ci_lab import grader, static_gates
from scripts.ci_lab.static_gates import WHOLE_TREE, StaticGate

ROOT = Path(__file__).resolve().parents[2]

WORKFLOW = r"""
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install ruff
        run: python -m pip install ruff==0.15.12
      - name: Lint check
        run: python -m ruff check hooks/ scripts/ tests/
      - name: Format check
        run: python -m ruff format --check hooks/ scripts/ tests/
      - name: Refuse marker key vector drift
        run: |
          python -m \
            scripts.ci_marker_vectors .marker-kernel/vectors.json
      - name: Hold the size and complexity limits
        run: |
          if [[ -f "$RUNNER_TEMP/grader/scripts/size_limits.py" ]]; then
            cd "$RUNNER_TEMP/grader"
          fi
          python -m scripts.size_limits --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
      - name: Keep private keywords out of public docs
        id: docs
        run: |
          scanner="$RUNNER_TEMP/grader/scripts/docs_sanity.py"
          if [[ ! -f "$scanner" ]]; then scanner=scripts/docs_sanity.py; fi
          python -I "$scanner" docs
      - name: Require trusted actors
        run: |
          if [[ -f "$RUNNER_TEMP/grader/scripts/ci_runner_trust.py" ]]; then
            cd "$RUNNER_TEMP/grader"
            python -m scripts.gate_protected --head "$GITHUB_SHA"
          else python -m scripts.ci_runner_trust; fi
      - run: python -m scripts.unnamed_check --strict
      - name: Prove the mutation gate self plant
        run: |
          grader="$RUNNER_TEMP/grader/tests/mutation_self_plant/plant.py"
          if [[ ! -f "$grader" ]]; then grader=tests/mutation_self_plant/plant.py; fi
          PYTHONPATH="$GITHUB_WORKSPACE" python "$grader" kill
      - name: Prepare a checker venv
        run: (python -m venv "$RUNNER_TEMP/columns" && "$RUNNER_TEMP/columns/bin/python" -m pip install PyYAML==6.0.3)
      - name: Run the foreign column checker
        run: |
          "$RUNNER_TEMP/columns/bin/python" .antoncore/scripts/columns.py check
  type-gate:
    runs-on: ubuntu-latest
    steps:
      - name: Hold the strict type checker
        run: python -m scripts.type_gate --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
      - name: Install basedpyright
        run: |
          uv venv --python "$PYTHON_PATH" "$HOME/venv"
          uv pip install basedpyright
      - name: Measure the type errors
        run: python -m basedpyright --outputjson
  semgrep:
    uses: ./.github/workflows/semgrep.yml
    with:
      base: origin/dev
  unit:
    runs-on: ubuntu-latest
    steps:
      - name: Plan the shards
        run: python -m scripts.ci_shards --plan
"""

SEMGREP = r"""
on: workflow_call
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - name: Install Semgrep
        run: |
          if [[ "$(semgrep --version)" != "$SEMGREP_VERSION" ]]; then
            python -m pip install "semgrep==$SEMGREP_VERSION"
          fi
      - name: Own rules hold
        run: |
          python -m pip install "semgrep==$SEMGREP_VERSION"
          semgrep scan --error --config "$RULES" .
      - name: Rule exceptions only shrink
        run: |
          LIST="$list" python - <<'PY'
          print("ok")
          PY
  charts:
    runs-on: ubuntu-latest
    steps:
      - name: Guard rules hold at their computed floors
        run: |
          grader=scripts/ci_semgrep_floors.py
          if git cat-file -e FETCH_HEAD:scripts/ci_semgrep_floors.py; then
            grader="$RUNNER_TEMP/ci_semgrep_floors.py"
          fi
          python "${grader}" check --base FETCH_HEAD
"""

REUSABLE = {"semgrep.yml": yaml.safe_load(SEMGREP)}

REGISTRY: dict[str, Any] = {
    "lint/lint-check": {"paths": ["hooks", "scripts"], "base_paths": ["scripts/size_limits.py"], "tools": ["ruff"]},
    "type-gate/measure-the-type-errors": {"paths": ["scripts"], "tools": ["basedpyright"]},
}

FIELDS = ("name", "job", "step", "tools", "modules", "paths", "base_paths")

STEPPED: dict[str, Any] = {
    "name": "Rule exceptions only shrink",
    "if": "${{ !cancelled() && steps.plan.outputs.changed == 'true' }}",
    "env": {"BASE": "${{ inputs.base }}", "RULES": ".semgrep", "TIMEOUT": 30},
    "run": 'python -m scripts.rule_exceptions --base "$BASE"',
}

EXPECTED = (
    ("lint/lint-check", "lint", "Lint check", ("ruff check",), (), ("hooks", "scripts"), ("scripts/size_limits.py",)),
    ("lint/format-check", "lint", "Format check", ("ruff format",), (), WHOLE_TREE, WHOLE_TREE),
    (
        "lint/refuse-marker-key-vector-drift",
        "lint",
        "Refuse marker key vector drift",
        ("python",),
        ("scripts.ci_marker_vectors",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/hold-the-size-and-complexity-limits",
        "lint",
        "Hold the size and complexity limits",
        ("python",),
        ("scripts.size_limits",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/docs",
        "lint",
        "Keep private keywords out of public docs",
        ("python",),
        ("scripts.docs_sanity",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/require-trusted-actors",
        "lint",
        "Require trusted actors",
        ("python",),
        ("scripts.ci_runner_trust", "scripts.gate_protected"),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/python-m-scripts-unnamed-check-strict",
        "lint",
        "python -m scripts.unnamed_check --strict",
        ("python",),
        ("scripts.unnamed_check",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/prove-the-mutation-gate-self-plant",
        "lint",
        "Prove the mutation gate self plant",
        ("python",),
        ("tests.mutation_self_plant.plant",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "lint/run-the-foreign-column-checker",
        "lint",
        "Run the foreign column checker",
        ("python",),
        (),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "type-gate/hold-the-strict-type-checker",
        "type-gate",
        "Hold the strict type checker",
        ("python",),
        ("scripts.type_gate",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "type-gate/measure-the-type-errors",
        "type-gate",
        "Measure the type errors",
        ("basedpyright",),
        (),
        ("scripts",),
        (),
    ),
    ("semgrep/scan/own-rules-hold", "semgrep/scan", "Own rules hold", ("semgrep",), (), WHOLE_TREE, WHOLE_TREE),
    (
        "semgrep/scan/rule-exceptions-only-shrink",
        "semgrep/scan",
        "Rule exceptions only shrink",
        ("python",),
        (),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
    (
        "semgrep/charts/guard-rules-hold-at-their-computed-floors",
        "semgrep/charts",
        "Guard rules hold at their computed floors",
        ("python",),
        ("scripts.ci_semgrep_floors",),
        WHOLE_TREE,
        WHOLE_TREE,
    ),
)

TREE = {
    "scripts/__init__.py": "",
    "scripts/gate.py": "import json\nfrom scripts import helper\nfrom . import sibling\n\n\ndef run() -> None:\n    import scripts.lazy\n",
    "scripts/helper.py": "from .deep.leaf import VALUE\n",
    "scripts/sibling.py": "from scripts import gate\n",
    "scripts/lazy.py": "thing = 1\n",
    "scripts/deep/__init__.py": "from .. import shared\n",
    "scripts/deep/leaf.py": "VALUE = 1\n",
    "scripts/shared.py": "",
    "scripts/unused.py": "",
    "scripts/tool/__init__.py": "from . import core\n",
    "scripts/tool/__main__.py": "from scripts.tool import cli\n",
    "scripts/tool/cli.py": "",
    "scripts/tool/core.py": "",
}

GATE_CLOSURE = (
    "scripts/__init__.py",
    "scripts/deep/__init__.py",
    "scripts/deep/leaf.py",
    "scripts/gate.py",
    "scripts/helper.py",
    "scripts/lazy.py",
    "scripts/shared.py",
    "scripts/sibling.py",
)


def _fields(gates: tuple[StaticGate, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(getattr(gate, field) for field in FIELDS) for gate in gates)


def _names(gates: tuple[StaticGate, ...]) -> list[str]:
    return [gate.name for gate in gates]


def _edited(text: str, job: str, steps: list[dict[str, str]]) -> str:
    workflow = yaml.safe_load(text)
    workflow["jobs"][job]["steps"] = steps
    return yaml.safe_dump(workflow, sort_keys=False)


def _steps(job: str) -> list[dict[str, str]]:
    return yaml.safe_load(WORKFLOW)["jobs"][job]["steps"]


def _tree(root: Path) -> Path:
    for path, text in TREE.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def _repository(root: Path) -> Path:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text(WORKFLOW)
    (workflows / "semgrep.yml").write_text(SEMGREP)
    (root / ".github" / "input-keys.json").write_text(json.dumps(REGISTRY))
    return root


def test_discover_lists_every_gate_step_of_the_static_jobs() -> None:
    assert _fields(static_gates.discover(WORKFLOW, REGISTRY, REUSABLE)) == EXPECTED


def test_a_gate_keeps_its_step_run_text() -> None:
    gates = {gate.name: gate for gate in static_gates.discover(WORKFLOW, REGISTRY, REUSABLE)}
    assert gates["lint/refuse-marker-key-vector-drift"].run == (
        "python -m \\\n  scripts.ci_marker_vectors .marker-kernel/vectors.json\n"
    )


def test_removing_a_step_drops_exactly_its_gate() -> None:
    kept = [step for step in _steps("lint") if step.get("name") != "Hold the size and complexity limits"]
    gates = static_gates.discover(_edited(WORKFLOW, "lint", kept), REGISTRY, REUSABLE)
    assert _names(gates) == [name for name, *_ in EXPECTED if name != "lint/hold-the-size-and-complexity-limits"]


def test_an_added_step_appears_with_no_code_change() -> None:
    added = {"name": "Hold a brand new gate", "run": 'python -m scripts.brand_new_gate --base "$B" --head "$H"'}
    gates = static_gates.discover(_edited(WORKFLOW, "type-gate", [*_steps("type-gate"), added]), REGISTRY, REUSABLE)
    assert _fields(gates)[:11] == EXPECTED[:11]
    assert _fields(gates)[11] == (
        "type-gate/hold-a-brand-new-gate",
        "type-gate",
        "Hold a brand new gate",
        ("python",),
        ("scripts.brand_new_gate",),
        WHOLE_TREE,
        WHOLE_TREE,
    )
    assert _fields(gates)[12:] == EXPECTED[11:]


def test_a_missing_static_job_drops_only_its_gates() -> None:
    workflow = yaml.safe_load(WORKFLOW)
    del workflow["jobs"]["type-gate"]
    gates = static_gates.discover(yaml.safe_dump(workflow), REGISTRY, REUSABLE)
    assert _names(gates) == [name for name, job, *_ in EXPECTED if job != "type-gate"]


def test_a_gate_absent_from_the_registry_keys_the_whole_trees() -> None:
    gates = static_gates.discover(WORKFLOW, reusable=REUSABLE)
    assert {(gate.paths, gate.base_paths) for gate in gates} == {(WHOLE_TREE, WHOLE_TREE)}
    assert WHOLE_TREE == (".",)


def test_a_reusable_workflow_not_given_is_refused() -> None:
    with pytest.raises(ValueError) as refused:
        static_gates.discover(WORKFLOW, REGISTRY)
    assert str(refused.value) == "semgrep uses ./.github/workflows/semgrep.yml, which was not given"


def test_setup_commands_are_not_gates() -> None:
    setup = [
        {"name": "Install a pinned tool", "run": 'python -m pip install "ruff==0.15.12" "semgrep==1.177.0"'},
        {"name": "Install an unpinned tool", "run": "uv pip install basedpyright semgrep"},
        {"name": "Make a venv", "run": 'uv venv --python "$PYTHON_PATH" "$HOME/venv" && python -m venv .venv'},
        {"name": "Probe versions", "run": "semgrep --version\nbasedpyright --version\nruff --version"},
    ]
    gates = static_gates.discover(_edited(WORKFLOW, "lint", setup), REGISTRY, REUSABLE)
    assert _names(gates) == [name for name, job, *_ in EXPECTED if job != "lint"]


def test_two_steps_with_one_gate_name_are_refused() -> None:
    twice = [*_steps("lint"), {"name": "Lint check", "run": "ruff check ."}]
    with pytest.raises(ValueError) as refused:
        static_gates.discover(_edited(WORKFLOW, "lint", twice), REGISTRY, REUSABLE)
    assert str(refused.value) == "lint/lint-check names two gate steps"


def test_discovery_over_the_real_workflow_lists_every_gate_the_lint_job_runs() -> None:
    workflows = ROOT / ".github" / "workflows"
    registry = json.loads((ROOT / ".github" / "input-keys.json").read_text())
    text = (workflows / "test.yml").read_text()
    gates = static_gates.discover(text, registry, ci_wiring.load(ROOT))
    lint = {gate.step: gate for gate in gates if gate.job == "lint"}
    for step in yaml.safe_load(text)["jobs"]["lint"]["steps"]:
        for module in re.findall(r"python -m (scripts\.[\w.]+)", step.get("run", "")):
            assert module in lint[step["name"]].modules
    assert {"ruff check", "ruff format"} <= {tool for gate in lint.values() for tool in gate.tools}
    assert ("scripts.type_gate",) in [gate.modules for gate in gates if gate.job == "type-gate"]
    semgrep = [gate for gate in gates if gate.job.startswith("semgrep/")]
    assert ("scripts.ci_semgrep_floors",) in [gate.modules for gate in semgrep]
    assert any("semgrep" in gate.tools for gate in semgrep)


def test_the_command_prints_one_json_line_per_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repository(tmp_path / "repo"))
    assert static_gates.main([]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {
            **{field: (list(value) if isinstance(value, tuple) else value) for field, value in zip(FIELDS, gate)},
            "env": {},
            "condition": "",
            "producers": [],
            "reads": [],
            "matrix": {},
            "missing": [],
        }
        for gate in EXPECTED
    ]


@pytest.mark.feature("lab-static-step-env")
def test_a_gate_carries_its_step_env_and_if_condition() -> None:
    gates = static_gates.discover(_edited(WORKFLOW, "type-gate", [STEPPED]), REGISTRY, REUSABLE)
    stepped = [gate for gate in gates if gate.job == "type-gate"]
    assert [gate.name for gate in stepped] == ["type-gate/rule-exceptions-only-shrink"]
    assert stepped[0].env == (("BASE", "${{ inputs.base }}"), ("RULES", ".semgrep"), ("TIMEOUT", "30"))
    assert stepped[0].condition == "${{ !cancelled() && steps.plan.outputs.changed == 'true' }}"
    assert {(gate.env, gate.condition) for gate in gates if gate.job != "type-gate"} == {((), "")}


@pytest.mark.feature("lab-static-step-env")
def test_yaml_booleans_in_step_env_and_if_read_as_github_writes_them() -> None:
    env = {"FLAG": False, "MODE": "Fast", "EMPTY": None}
    flagged = {"name": "Flagged gate", "if": True, "env": env, "run": "python -m scripts.flagged"}
    gates = static_gates.discover(_edited(WORKFLOW, "type-gate", [flagged]), REGISTRY, REUSABLE)
    assert [(gate.env, gate.condition) for gate in gates if gate.job == "type-gate"] == [
        ((("FLAG", "false"), ("MODE", "Fast"), ("EMPTY", "")), "true")
    ]


@pytest.mark.feature("lab-static-step-env")
def test_the_command_prints_each_gate_step_env_and_if_condition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / static_gates.WORKFLOW).write_text(_edited(WORKFLOW, "type-gate", [STEPPED]))
    assert static_gates.main(["--root", str(repository)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    stepped = [line for line in lines if line["job"] == "type-gate"]
    assert [(line["env"], line["condition"]) for line in stepped] == [
        ({"BASE": "${{ inputs.base }}", "RULES": ".semgrep", "TIMEOUT": "30"}, STEPPED["if"])
    ]


@pytest.mark.feature("lab-static-step-env")
def test_the_real_semgrep_scan_and_rule_exception_gates_carry_the_env_they_read() -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = {gate.name: gate for gate in static_gates.discover(workflow, registry, ci_wiring.load(ROOT))}
    assert ("RULES", ".semgrep") in gates["semgrep/scan/own-rules-hold-on-their-fixtures-and-the-code"].env
    assert ("BASE", "${{ inputs.base }}") in gates["semgrep/scan/rule-exceptions-only-shrink"].env
    assert "!cancelled()" in gates["lint/lint-check"].condition


PRODUCED = r"""
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Pick the pages
        id: pick
        env:
          BASE: ${{ inputs.base }}
        run: echo "browser=true" >> "$GITHUB_OUTPUT"
      - name: Mint a token
        id: token
        if: ${{ github.event_name == 'pull_request' }}
        uses: actions/create-github-app-token@v3
      - name: Fetch with the token
        id: fetch
        if: ${{ !cancelled() && steps.token.outcome == 'success' }}
        env:
          GH_TOKEN: ${{ steps.token.outputs.token }}
        run: echo "revision=abc" >> "$GITHUB_OUTPUT"
      - name: Unrelated
        id: unrelated
        run: echo "x=1" >> "$GITHUB_OUTPUT"
      - name: Check the pages
        if: ${{ !cancelled() && steps.pick.outputs.browser == 'true' }}
        env:
          REVISION: ${{ steps.fetch.outputs.revision }}
          LATER: ${{ steps.later.outputs.value }}
        run: python -m scripts.check_pages
      - name: Later
        id: later
        run: echo "value=1" >> "$GITHUB_OUTPUT"
"""

READ_ALL = r"""
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Pick
        id: pick
        run: echo "browser=true" >> "$GITHUB_OUTPUT"
      - name: Read everything
        env:
          A: ${{ inputs.base }} ${{ github.sha }} ${{ github.event.pull_request.head.sha }}
          B: ${{ github.event.pull_request.base.sha }} ${{ github.run_id }} ${{ github.run_attempt }}
          C: ${{ github.run_number }} ${{ secrets.ANY_NAME }} ${{ steps.pick.outcome }} ${{ steps.pick.conclusion }}
          D: ${{ steps.pick.outputs.browser }} ${{ steps.pick.result }} ${{ github.head_ref }} ${{ github.event_name }}
          E: ${{ needs.reuse.outputs.grader }} ${{ matrix.python-version }} ${{ vars.X }} ${{ secretsX.Y }}
          F: ${{ runner.temp }}
        run: python -m scripts.read_everything
"""


@pytest.mark.feature("lab-static-step-env")
def test_a_gate_carries_the_earlier_steps_its_expressions_read_in_job_order() -> None:
    (gate,) = static_gates.discover(PRODUCED)
    assert gate.name == "lint/check-the-pages"
    assert gate.producers == (
        static_gates.Producer("pick", 'echo "browser=true" >> "$GITHUB_OUTPUT"', (("BASE", "${{ inputs.base }}"),)),
        static_gates.Producer("token", "", (), "${{ github.event_name == 'pull_request' }}"),
        static_gates.Producer(
            "fetch",
            'echo "revision=abc" >> "$GITHUB_OUTPUT"',
            (("GH_TOKEN", "${{ steps.token.outputs.token }}"),),
            "${{ !cancelled() && steps.token.outcome == 'success' }}",
        ),
    )
    assert gate.reads == (
        "inputs.base",
        "steps.fetch.outputs.revision",
        "steps.later.outputs.value",
        "steps.pick.outputs.browser",
        "steps.token.outcome",
        "steps.token.outputs.token",
    )
    assert gate.missing == ("steps.later.outputs.value",)


@pytest.mark.feature("lab-static-step-env")
def test_a_gate_misses_every_name_outside_the_lease_fields_its_secrets_and_its_earlier_steps() -> None:
    (gate,) = static_gates.discover(READ_ALL)
    assert static_gates.LEASE_FIELDS == {
        "inputs.base",
        "github.sha",
        "github.event.pull_request.head.sha",
        "github.event.pull_request.base.sha",
        "github.event_name",
        "github.run_id",
        "github.run_attempt",
        "github.run_number",
    }
    assert gate.missing == (
        "github.head_ref",
        "matrix.python-version",
        "needs.reuse.outputs.grader",
        "secretsX.Y",
        "steps.pick.result",
        "vars.X",
    )


@pytest.mark.feature("lab-static-step-env")
def test_the_real_workflow_marks_each_gate_reading_an_event_field_the_lease_lacks_not_lab_gradable() -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    assert {gate.name: gate.missing for gate in gates if gate.missing} == {
        "lint/refuse-changelog-fragments-named-off-the-branch-or-opening-without-a-section-heading": (
            "github.head_ref",
            "github.ref_name",
        ),
        "lint/artifact-sanity-checks": (
            "github.base_ref",
            "github.event.before",
            "github.event.merge_group.base_sha",
        ),
    }


@pytest.mark.feature("lab-static-step-env")
def test_the_command_prints_each_gate_producers_reads_and_missing_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / static_gates.WORKFLOW).write_text(PRODUCED)
    assert static_gates.main(["--root", str(repository)]) == 0
    (line,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (line["producers"], line["missing"]) == (["pick", "token", "fetch"], ["steps.later.outputs.value"])
    assert line["reads"][0] == "inputs.base"
    assert "run" not in line


DOCS_REASON = "builds the pages with the Jekyll image under docker run, and lab units run with no Docker daemon"
WRITER_LOCK_REASON = (
    "runs a Postgres container and calls nested reusable workflows, and lab units run with no Docker daemon or network"
)

SUPPLIED = r"""
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12"]
        shard: [1, 2]
    steps:
      - name: Read the run
        if: ${{ github.event_name == 'pull_request' }}
        env:
          COUNTS: ${{ runner.temp }}/counts
          PYTHON: ${{ matrix.python-version }}
        run: python -m scripts.read_the_run
      - name: Read the shard
        env:
          SHARD: ${{ matrix.shard }}
        run: python -m scripts.read_the_shard
  type-gate:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12"]
        include:
          - python-version: "3.13"
    steps:
      - name: Read an included version
        env:
          PYTHON: ${{ matrix.python-version }}
        run: python -m scripts.read_the_version
  test-count:
    runs-on: ubuntu-latest
    strategy:
      matrix: ${{ fromJSON(needs.reuse.outputs.versions) }}
    steps:
      - name: Read a computed version
        env:
          PYTHON: ${{ matrix.python-version }}
        run: python -m scripts.read_the_version
"""

CALLER = r"""
on: pull_request
jobs:
  lint:
    uses: ./.github/workflows/nested.yml
  docs:
    runs-on: ubuntu-latest
    steps:
      - name: Build the pages
        run: docker run --rm ghcr.io/actions/jekyll-build-pages:v1.0.13
  writer-lock:
    uses: ./.github/workflows/writer-lock.yml
"""

NESTED = r"""
on: workflow_call
jobs:
  smoke:
    uses: ./.github/workflows/smoke.yml
  scan:
    runs-on: ubuntu-latest
    steps:
      - name: Scan the tree
        run: python -m scripts.scan_tree
"""


@pytest.mark.feature("lab-static-test-count-units")
def test_a_gate_misses_no_run_folder_event_name_or_matrix_value_its_job_holds_at_one_value() -> None:
    gates = {gate.name: gate.missing for gate in static_gates.discover(SUPPLIED)}
    assert gates == {
        "lint/read-the-run": (),
        "lint/read-the-shard": ("matrix.shard",),
        "type-gate/read-an-included-version": ("matrix.python-version",),
        "test-count/read-a-computed-version": ("matrix.python-version",),
    }


@pytest.mark.feature("lab-static-test-count-units")
def test_the_command_prints_the_matrix_values_each_gate_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / static_gates.WORKFLOW).write_text(SUPPLIED)
    assert static_gates.main(["--root", str(repository)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {line["name"]: line["matrix"] for line in lines} == {
        "lint/read-the-run": {"matrix.python-version": "3.12"},
        "lint/read-the-shard": {},
        "type-gate/read-an-included-version": {},
        "test-count/read-a-computed-version": {},
    }


@pytest.mark.feature("lab-static-test-count-units")
def test_discovery_grades_the_steps_of_a_reusable_workflow_past_a_job_that_calls_another() -> None:
    gates = static_gates.discover(CALLER, reusable={"nested.yml": yaml.safe_load(NESTED)})
    assert _names(gates) == ["lint/scan/scan-the-tree"]


@pytest.mark.feature("lab-static-test-count-units")
def test_the_command_names_each_job_the_lab_leaves_to_actions_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path / "repo")
    (repository / static_gates.WORKFLOW).write_text(CALLER)
    (repository / ".github" / "workflows" / "nested.yml").write_text(NESTED)
    assert static_gates.main(["--root", str(repository), "--ci-only"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"job": "lint/smoke", "reason": "calls the reusable workflow ./.github/workflows/smoke.yml"},
        {"job": "docs", "reason": DOCS_REASON},
        {"job": "writer-lock", "reason": WRITER_LOCK_REASON},
    ]


@pytest.mark.feature("lab-static-test-count-units")
def test_the_real_test_count_job_yields_runnable_gates_while_docs_and_writer_lock_stay_ci_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    assert {gate.name: gate.missing for gate in gates if gate.job == "test-count"} == {
        "test-count/install-dependencies": (),
        "test-count/fetch-the-base-suite-count-recorded-on-dev": (),
        "test-count/hold-the-base-revision-s-test-count": (),
        "test-count/record-the-head-count-for-the-next-queue-group": (),
    }
    assert static_gates.main(["--root", str(ROOT), "--ci-only"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {line["job"]: line["reason"] for line in lines} == {"docs": DOCS_REASON, "writer-lock": WRITER_LOCK_REASON}
    assert len(lines) == 2


@pytest.mark.feature("lab-static-test-count-units")
def test_every_lab_gradable_gate_that_reads_the_event_name_is_one_whose_verdict_the_pull_request_event_keeps() -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    assert sorted(gate.name for gate in gates if not gate.missing and "github.event_name" in gate.reads) == [
        "lint/prove-the-mutation-gate-kills-with-the-real-test-and-fails-with-a-weakened-one",
        "test-count/fetch-the-base-suite-count-recorded-on-dev",
        "test-count/record-the-head-count-for-the-next-queue-group",
    ]


def test_the_command_adds_each_gate_closure_digest_at_the_grader_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path / "repo")
    tree = _tree(tmp_path / "grader")
    (tree / "scripts" / "size_limits.py").write_text("from scripts import gate\n")
    assert static_gates.main(["--root", str(repository), "--grader", str(tree)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["name"] for line in lines] == [name for name, *_ in EXPECTED]
    assert [line["digest"] for line in lines] == [grader.digest(tree, line["modules"]) for line in lines]
    sized = next(line for line in lines if line["name"] == "lint/hold-the-size-and-complexity-limits")
    assert sized["digest"] != grader.digest(tree, ())


def test_revision_is_the_pull_request_base_sha() -> None:
    payload = {"pull_request": {"base": {"sha": "b4se"}, "head": {"sha": "he4d"}}, "after": "aft3r"}
    assert grader.revision(payload, "d3vhead") == "b4se"


def test_revision_of_a_push_is_the_dev_head() -> None:
    assert grader.revision({"after": "aft3r", "ref": "refs/heads/dev"}, "d3vhead") == "d3vhead"


def test_closure_follows_absolute_relative_function_level_and_parent_imports(tmp_path: Path) -> None:
    root = _tree(tmp_path / "tree")
    assert grader.closure(root, ["scripts.gate"]) == GATE_CLOSURE
    assert grader.closure(root, ["scripts.sibling"]) == GATE_CLOSURE
    assert grader.closure(root, ["scripts.lazy"]) == ("scripts/__init__.py", "scripts/lazy.py")
    assert grader.closure(root, ["json"]) == ()


def test_closure_of_a_package_runs_its_main(tmp_path: Path) -> None:
    root = _tree(tmp_path / "tree")
    assert grader.closure(root, ["scripts.tool"]) == (
        "scripts/__init__.py",
        "scripts/tool/__init__.py",
        "scripts/tool/__main__.py",
        "scripts/tool/cli.py",
        "scripts/tool/core.py",
    )


def test_digest_covers_the_closure_files_and_nothing_else(tmp_path: Path) -> None:
    root = _tree(tmp_path / "tree")
    before = grader.digest(root, ["scripts.gate"])
    record = [[path, hashlib.sha256((root / path).read_bytes()).hexdigest()] for path in GATE_CLOSURE]
    assert before == hashlib.sha256(json.dumps(record).encode()).hexdigest()
    (root / "scripts" / "unused.py").write_text("CHANGED = 1\n")
    assert grader.digest(root, ["scripts.gate"]) == before
    (root / "scripts" / "deep" / "leaf.py").write_text("VALUE = 2\n")
    assert grader.digest(root, ["scripts.gate"]) != before
