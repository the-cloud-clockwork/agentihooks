import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.ledger_load import paired

ROOT = Path(__file__).resolve().parents[2]
BASE = {"seconds": 90, "errors": 0, "write_p50_s": 1.0, "busiest_cores": 0.5}


def log(*measures: dict) -> str:
    return "\n".join(["ledger load-gate: 1000 tasks", *(json.dumps(measure) for measure in measures), "done"])


def test_one_noisy_pair_cannot_turn_the_median_pair_ratio_red():
    pairs = [
        (BASE, {**BASE, "write_p50_s": 1.4, "busiest_cores": 0.7}),
        (BASE, BASE),
        (BASE, {**BASE, "write_p50_s": 3.0}),
    ]
    assert paired.regressions(pairs, 1.5) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"errors": 1}, "head had 2 failed requests, base 0"),
        ({"write_p50_s": 2.0}, "head write p50 median pair ratio 2.00 exceeds the 1.5x margin"),
        ({"busiest_cores": 0.8}, "head busiest CPU window median pair ratio 1.60 exceeds the 1.5x margin"),
    ],
)
def test_a_head_regression_in_most_pairs_is_named(change, problem):
    head = {**BASE, **change}
    assert paired.regressions([(BASE, head), (BASE, head), (BASE, BASE)], 1.5) == [problem]


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
        f"head write p50 median pair ratio 3.00 exceeds the {margin}x margin"
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
    assert "::error::head busiest CPU window median pair ratio 2.00 exceeds the 1.5x margin" in capsys.readouterr().out
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
    upload = next(step for step in compare["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["name"] == "compare-pair-${{ matrix.pair }}"
    assert upload["if"] == "always()"
