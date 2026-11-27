import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.ledger_load import paired

ROOT = Path(__file__).resolve().parents[2]
BASE = {"seconds": 90, "errors": 0, "write_p50_s": 1.0, "busiest_cores": 0.5}


def log(*measures: dict) -> str:
    return "\n".join(["ledger load-gate: 1000 tasks", *(json.dumps(measure) for measure in measures), "done"])


def test_one_noisy_pair_cannot_turn_the_order_balanced_pair_ratio_red():
    pairs = [
        (BASE, {**BASE, "write_p50_s": 1.4, "busiest_cores": 0.7}),
        (BASE, BASE),
        (BASE, {**BASE, "write_p50_s": 3.0}),
    ]
    assert paired.regressions(pairs, 1.5) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"errors": 1}, "head had 4 failed requests, base 0"),
        ({"write_p50_s": 2.0}, "head write p50 order balanced pair ratio 2.00 exceeds the 1.5x margin"),
        ({"busiest_cores": 0.8}, "head busiest CPU window order balanced pair ratio 1.60 exceeds the 1.5x margin"),
    ],
)
def test_a_head_regression_in_most_pairs_is_named(change, problem):
    head = {**BASE, **change}
    assert paired.regressions([(BASE, head), (BASE, head), (BASE, head), (BASE, head), (BASE, BASE)], 1.5) == [problem]


def measures(*rows: tuple[float, float, float, float]) -> list[tuple[dict, dict]]:
    return [
        (
            {"errors": 0, "write_p50_s": base_write, "busiest_cores": base_cpu},
            {"errors": 0, "write_p50_s": head_write, "busiest_cores": head_cpu},
        )
        for base_write, base_cpu, head_write, head_cpu in rows
    ]


def test_a_slower_second_run_cannot_turn_the_three_base_first_pairs_red():
    pairs = measures(
        (0.107, 0.146, 0.142, 0.157),
        (0.158, 0.164, 0.115, 0.169),
        (0.11, 0.153, 0.151, 0.167),
        (0.156, 0.162, 0.126, 0.17),
        (0.121, 0.173, 0.199, 0.173),
    )
    assert paired.regressions(pairs, 1.25) == []


def test_a_head_slower_whichever_side_runs_first_is_named():
    pairs = measures(*[(0.1, 0.15, 0.15, 0.15), (0.15, 0.15, 0.2, 0.15)] * 2, (0.1, 0.15, 0.15, 0.15))
    assert paired.regressions(pairs, 1.25) == ["head write p50 order balanced pair ratio 1.41 exceeds the 1.25x margin"]


def test_a_measure_missing_from_any_run_is_named():
    head = {**BASE, "write_p50_s": None}
    assert paired.regressions([(BASE, head), (BASE, BASE)], 1.5) == [
        "write p50 was not measured on the head run of pair 1"
    ]


def test_a_pair_missing_its_run_is_named():
    assert paired.regressions([(None, BASE), (BASE, BASE)], 1.5) == ["the base run of pair 1 printed no load measure"]
    assert paired.regressions([(BASE, None)], 1.5) == ["the head run of pair 1 printed no load measure"]
    assert paired.regressions([], 1.5) == ["no paired runs were given"]
    assert paired.regressions([({**BASE, "busiest_cores": 0.0}, BASE)], 1.5) == [
        "busiest CPU window was not measured on the base run of pair 1"
    ]
    assert paired.regressions([(BASE, BASE), ({**BASE, "write_p50_s": 2.0}, {**BASE, "write_p50_s": 0.0})], 1.5) == [
        "write p50 was not measured on the head run of pair 2"
    ]


def test_the_same_code_bound_is_the_widest_single_pair_ratio_in_either_direction():
    fast = {**BASE, "write_p50_s": 0.5}
    runs = [[(BASE, BASE), (BASE, fast), (BASE, fast)], [(BASE, {**BASE, "busiest_cores": 0.6})]]
    assert paired.same_code_bound(runs) == 2.0
    assert paired.same_code_bound([]) == 1.0


def test_the_recorded_same_code_runs_fit_inside_the_margin_the_workflow_states():
    workflow = yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())
    margin = float(workflow["jobs"]["grade"]["env"]["SAME_CODE_MARGIN"])
    recorded = paired.recorded_runs(ROOT / "tests/ledger_load/same_code_pairs.json")
    assert len(recorded) >= 3
    assert paired.same_code_bound(recorded) <= margin


def test_each_load_pair_job_allows_as_long_as_the_hierarchy_job_that_runs_the_same_install() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())["jobs"]
    load, hierarchy = jobs["load"], jobs["hierarchy"]
    install = "Install dependencies shared by both revisions"
    assert [step["run"] for step in load["steps"] if step.get("name") == install] == [
        step["run"] for step in hierarchy["steps"] if step.get("name") == install
    ]
    assert load["runs-on"] == hierarchy["runs-on"]
    assert load["timeout-minutes"] >= hierarchy["timeout-minutes"]


def test_both_hierarchy_cpu_installs_pin_the_pretend_version() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())["jobs"]
    for name in ("load", "hierarchy"):
        [install] = [
            step for step in jobs[name]["steps"] if step.get("name") == "Install dependencies shared by both revisions"
        ]
        assert (
            install.get("env", {}).get("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS")
            == "0.dev${{ github.run_number }}"
        )


@pytest.mark.parametrize(("stray", "other"), [("", ""), ("base", "head"), ("head", "base")])
def test_a_load_side_that_imports_the_other_checkout_fails_the_pair(tmp_path: Path, stray: str, other: str) -> None:
    load = yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())["jobs"]["load"]
    [command] = [step["run"] for step in load["steps"] if "tests.ledger_load.memory" in step.get("run", "")]
    workspace = tmp_path / "workspace"
    (workspace / "load-evidence").mkdir(parents=True)
    for side in ("base", "head"):
        (workspace / side).mkdir()
        if side != stray:
            package = workspace / side / "scripts" / "swarm_ledger"
            package.mkdir(parents=True)
            for module in (package.parent / "__init__.py", package / "__init__.py", package / "ledger_server.py"):
                module.touch()
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text('#!/usr/bin/env bash\n[[ "$1" != -c ]] || exec "$REAL_PYTHON" "$@"\necho "${PWD##*/} gate ran"\n')
    python.chmod(0o755)
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    if other:
        env["PYTHONPATH"] = str(workspace / other)
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=workspace,
        env={
            **env,
            "PATH": f"{binary}:{env['PATH']}",
            "REAL_PYTHON": sys.executable,
            "GITHUB_WORKSPACE": str(workspace),
            "PAIR": "1",
            "BASE": "base",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    logs = sorted((workspace / "load-evidence").glob("*.log"))
    ran = [path.stem for path in logs if "gate ran" in path.read_text()]
    if stray:
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"::error::load gate failed on {stray}" in result.stdout
        assert ran == [other]
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert ran == ["base", "head"]


OWN_CHECKOUT = (
    "-c 'import pathlib, scripts.swarm_ledger.ledger_server as m; "
    "assert pathlib.Path(m.__file__).resolve().is_relative_to(pathlib.Path.cwd().resolve()), m.__file__'"
)
LOAD_GATES = ("tests.ledger_load.gate", "tests.ledger_load.memory")


def guards_its_gate(script: str) -> bool:
    """Whether a step script runs the own checkout assert before its load gate with nothing between them that
    swallows the assert's failure."""
    gate = min(script.index(module) for module in LOAD_GATES if module in script)
    start = script.find(OWN_CHECKOUT)
    between = script[start + len(OWN_CHECKOUT) : gate]
    return 0 <= start < gate and "||" not in between and ";" not in between


@pytest.mark.parametrize(
    ("script", "guarded"),
    [
        (f"cd base && python {OWN_CHECKOUT} \\\n  && python -m tests.ledger_load.memory", True),
        ("cd base && python -m tests.ledger_load.memory", False),
        (f"cd base && python {OWN_CHECKOUT} || true\npython -m tests.ledger_load.memory", False),
        (f"cd base && python {OWN_CHECKOUT}; python -m tests.ledger_load.memory", False),
        (f"cd base && python -m tests.ledger_load.memory && python {OWN_CHECKOUT}", False),
    ],
)
def test_the_guard_check_rejects_a_missing_late_or_swallowed_assert(script: str, guarded: bool) -> None:
    assert guards_its_gate(script) is guarded


def test_every_load_pair_runner_asserts_each_side_imports_its_own_checkout_before_its_gate() -> None:
    runners: dict[str, bool] = {}
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            for step in job.get("steps", []):
                script = step.get("run", "")
                if "for side in" in script and any(module in script for module in LOAD_GATES):
                    runners[f"{path.name} {name}"] = guards_its_gate(script)
    assert runners == {"hierarchy-cpu.yml load": True, "ledger-load.yml compare": True}


def compare_mode() -> tuple[int, float]:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ledger-load.yml").read_text())["jobs"]
    pairs = jobs.get("compare", {}).get("strategy", {}).get("matrix", {}).get("pair", [1, 2])
    return len(pairs), float(jobs.get("compare-grade", {}).get("env", {}).get("SAME_CODE_MARGIN", paired.MARGIN))


def test_compare_mode_passes_one_same_code_pair_two_times_apart_and_fails_a_three_times_slower_write():
    count, margin = compare_mode()
    apart = {**BASE, "write_p50_s": 2.0, "busiest_cores": 1.0}
    assert paired.regressions([(BASE, apart), *[(BASE, BASE)] * (count - 1)], margin) == []
    slower = {**BASE, "write_p50_s": 3.0}
    assert paired.regressions([(BASE, slower)] * count, margin) == [
        f"head write p50 order balanced pair ratio 3.00 exceeds the {margin}x margin"
    ]


def test_compare_mode_records_its_own_same_code_runs_inside_the_margin_its_grade_states():
    count, margin = compare_mode()
    recorded = paired.recorded_runs(ROOT / "tests/ledger_load/compare_same_code_pairs.json")
    assert len(recorded) >= 3
    assert all(len(pairs) == count for pairs in recorded)
    assert paired.same_code_bound(recorded) <= margin
    grade = yaml.safe_load((ROOT / ".github/workflows/ledger-load.yml").read_text())["jobs"]["compare-grade"]
    run = next(step["run"] for step in grade["steps"] if "tests.ledger_load.paired" in step.get("run", ""))
    assert '--margin "$SAME_CODE_MARGIN" --same-code tests/ledger_load/compare_same_code_pairs.json' in run


def test_the_graded_measure_is_the_repeat_when_the_gate_measured_twice():
    repeat = {**BASE, "write_p50_s": 0.9}
    assert paired.graded_measure(log({**BASE, "write_p50_s": 3.0}, repeat)) == repeat
    assert paired.graded_measure("no measure here") is None


def test_the_command_pairs_runs_in_order_and_refuses_a_margin_below_the_same_code_bound(tmp_path, capsys):
    base, head, slow = tmp_path / "base.log", tmp_path / "head.log", tmp_path / "slow.log"
    same_code = tmp_path / "same_code.json"
    base.write_text(log(BASE))
    head.write_text(log(BASE))
    slow.write_text(log({**BASE, "busiest_cores": 1.0}))
    same_code.write_text(json.dumps({"runs": {"1": [[BASE, {**BASE, "write_p50_s": 1.3}]]}}))
    graded = ["--margin", "1.5", "--same-code", str(same_code)]
    assert (
        paired.main(["--base", str(base), str(base), str(base), "--head", str(head), str(head), str(slow), *graded])
        == 0
    )
    assert (
        paired.main(["--base", str(base), str(base), str(base), "--head", str(head), str(slow), str(slow), *graded])
        == 1
    )
    assert (
        "::error::head busiest CPU window order balanced pair ratio 1.73 exceeds the 1.5x margin"
        in capsys.readouterr().out
    )
    assert (
        paired.main(["--base", str(base), "--head", str(head), "--margin", "1.2", "--same-code", str(same_code)]) == 1
    )
    assert "::error::the margin 1.2x is below 1.30x" in capsys.readouterr().out
    assert paired.main(["--base", str(tmp_path / "absent.log"), "--head", str(head)]) == 1
    assert "::error::the base run of pair 1 printed no load measure" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        paired.main(["--base", str(base), "--head", str(head), str(head)])


@pytest.mark.parametrize("failed_side", ["", "base", "head"])
def test_compare_runs_both_windows_together_and_fails_when_either_side_fails(tmp_path, failed_side):
    jobs = yaml.safe_load((ROOT / ".github/workflows/ledger-load.yml").read_text())["jobs"]
    compare = jobs["compare"]
    assert compare["strategy"]["matrix"] == {"pair": [1, 2, 3, 4, 5]}
    assert compare["strategy"]["fail-fast"] is False
    command = next(step["run"] for step in compare["steps"] if "tests.ledger_load.gate" in step.get("run", ""))
    workspace = tmp_path / "head"
    (workspace / "load-base").mkdir(parents=True)
    (workspace / "load-evidence").mkdir()
    binary = tmp_path / "venv/bin"
    binary.mkdir(parents=True)
    python = binary / "python"
    python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" != -c ]] || exit 0\n'
        'side=head\n[[ "${PWD##*/}" != load-base ]] || side=base\n'
        'touch "$GITHUB_WORKSPACE/load-evidence/$side.started"\n'
        "for attempt in {1..100}; do\n"
        '  [[ ! -f "$GITHUB_WORKSPACE/load-evidence/base.started" || ! -f "$GITHUB_WORKSPACE/load-evidence/head.started" ]] || break\n'
        "  sleep 0.01\n"
        "done\n"
        '[[ -f "$GITHUB_WORKSPACE/load-evidence/base.started" && -f "$GITHUB_WORKSPACE/load-evidence/head.started" ]] || exit 21\n'
        'echo "$side window ran"\n'
        '[[ "$FAIL_SIDE" != "$side" ]]\n'
    )
    python.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=workspace,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "GITHUB_WORKSPACE": str(workspace),
            "FAIL_SIDE": failed_side,
            "PAIR": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) is (not failed_side), result.stdout + result.stderr
    for side in ("base", "head"):
        assert (workspace / f"load-evidence/{side}.log").read_text() == f"{side} window ran\n"
    upload = next(step for step in compare["steps"] if step.get("uses") == "./.github/actions/retry-upload")
    assert upload["with"]["name"] == "compare-pair-${{ matrix.pair }}"
    assert upload["if"] == "always()"


@pytest.mark.feature("ledger-load-server-resources")
@pytest.mark.parametrize(("workflow", "job"), [("hierarchy-cpu.yml", "load"), ("ledger-load.yml", "compare")])
def test_each_pair_upload_keeps_the_logs_of_both_ledger_servers_of_each_side(
    tmp_path: Path, workflow: str, job: str
) -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job]["steps"]
    [upload] = [step for step in steps if step.get("uses", "").endswith(".github/actions/retry-upload")]
    expected = {
        Path("load-evidence") / side / attempt / log
        for side in paired.SIDES
        for attempt in ("", "repeat")
        for log in ("server.log", "server-read.log")
    } | {Path(f"load-evidence/{side}.log") for side in paired.SIDES}
    for relative in expected:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("measurement\n")
    kept = {
        path.relative_to(tmp_path)
        for pattern in upload["with"]["path"].split()
        for path in tmp_path.glob(pattern)
        if path.is_file()
    }
    assert kept == expected
