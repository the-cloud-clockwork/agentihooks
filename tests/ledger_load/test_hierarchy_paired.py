import json
from pathlib import Path

import pytest
import yaml

from tests.ledger_load import hierarchy_paired

ROOT = Path(__file__).resolve().parents[2]
OUTPUTS = {"hierarchy": ["{}"]}


def report(cpu: float, outputs: dict = OUTPUTS) -> dict:
    return {"source": "corpus", "requests": 16, "mean_cpu_seconds": cpu, "observations": [{"outputs": outputs}]}


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())


def test_a_runner_order_bias_cancels_across_alternating_pairs():
    pairs = [(report(0.08), report(0.04)), (report(0.04), report(0.08))] * 2
    assert hierarchy_paired.regressions(pairs, 1.3) == []


def test_a_doubled_head_cpu_fails_while_the_first_run_of_each_pair_costs_twice():
    pairs = [(report(0.08), report(0.08)), (report(0.04), report(0.16))] * 2
    assert hierarchy_paired.regressions(pairs, 1.3) == [
        "head hierarchy CPU geometric mean pair ratio 2.00 exceeds the 1.3x margin"
    ]


def test_changed_bytes_and_missing_reports_are_named():
    changed = report(0.04, {"hierarchy": ["[]"]})
    assert hierarchy_paired.regressions([(report(0.04), changed)], 1.3) == [
        "hierarchy responses of pair 1 differ between base and head"
    ]
    assert hierarchy_paired.regressions([(None, report(0.04))], 1.3) == [
        "the base run of pair 1 wrote no hierarchy report"
    ]
    assert hierarchy_paired.regressions([], 1.3) == ["no paired runs were given"]
    assert hierarchy_paired.regressions([(report(0.0), report(0.04))], 1.3) == [
        "hierarchy CPU was not measured on the base run of pair 1"
    ]
    assert hierarchy_paired.regressions([(report(0.04), report(None))], 1.3) == [
        "hierarchy CPU was not measured on the head run of pair 1"
    ]


def test_the_same_code_bound_is_the_widest_geometric_mean_in_either_direction():
    assert hierarchy_paired.same_code_bound([[[0.08, 0.04], [0.04, 0.08]], [[0.05, 0.04]]]) == pytest.approx(1.25)
    assert hierarchy_paired.same_code_bound([]) == 1.0


def test_the_recorded_same_code_runs_fit_inside_the_margin_the_workflow_states():
    margin = float(workflow()["jobs"]["grade"]["env"]["HIERARCHY_SAME_CODE_MARGIN"])
    recorded = hierarchy_paired.recorded_runs(ROOT / "tests/ledger_load/hierarchy_same_code.json")
    assert len(recorded) >= 3
    assert hierarchy_paired.same_code_bound(recorded) <= margin < 2
    for pairs in recorded:
        assert hierarchy_paired.regressions([(report(base), report(head)) for base, head in pairs], margin) == []
    halving = [head <= base / 2 for pairs in recorded for base, head in pairs[::2]]
    assert not all(halving)


def test_the_workflow_runs_an_even_number_of_hierarchy_pairs_against_the_merge_base():
    job = workflow()["jobs"]["hierarchy"]
    pairs = job["strategy"]["matrix"]["pair"]
    assert pairs == [1, 2, 3, 4]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert 'order=(base head)\n[[ "$PAIR" == [13] ]] || order=(head base)' in script
    assert (
        '[[ -n "$BASE" ]] || BASE="$BEFORE"\n[[ -n "$BASE" ]] || BASE=$(git -C head merge-base HEAD origin/dev)'
        in script
    )
    resolve = next(step for step in job["steps"] if step.get("id") == "base")
    assert resolve["env"]["BEFORE"] == "${{ github.event_name == 'push' && github.event.before || '' }}"
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert workflow()[True]["workflow_call"]["inputs"]["base"]["default"] == ""
    assert "did not halve" not in script


def test_redis_installs_from_the_distribution_archive_alone():
    for name in ("hierarchy", "load"):
        steps = workflow()["jobs"][name]["steps"]
        start = next(step["run"] for step in steps if step.get("name") == "Start Redis on this runner")
        assert (
            'only_archive=(-o Acquire::Retries=5 -o "Dir::Etc::sourcelist=$archive" -o Dir::Etc::sourceparts=-)'
            in start
        )
        apt = [line for line in start.splitlines() if "apt-get" in line]
        assert len(apt) == 2 and all('"${only_archive[@]}"' in line for line in apt), name


def test_the_command_grades_pair_folders_and_refuses_a_margin_below_the_same_code_bound(tmp_path, capsys):
    folders = []
    for number, (base, head) in enumerate([(0.08, 0.04), (0.04, 0.08)], 1):
        folder = tmp_path / f"pair-{number}"
        folder.mkdir()
        (folder / "base.json").write_text(json.dumps(report(base)))
        (folder / "head.json").write_text(json.dumps(report(head)))
        folders.append(str(folder))
    same_code = tmp_path / "same_code.json"
    same_code.write_text(json.dumps({"runs": {"1": [[0.05, 0.06]]}}))
    assert hierarchy_paired.main(["--pairs", *folders, "--margin", "1.3", "--same-code", str(same_code)]) == 0
    assert hierarchy_paired.main(["--pairs", *folders, "--margin", "1.1", "--same-code", str(same_code)]) == 1
    assert "::error::the margin 1.1x is below 1.20x" in capsys.readouterr().out
    assert hierarchy_paired.main(["--pairs", str(tmp_path / "absent")]) == 1
    assert "::error::the base run of pair 1 wrote no hierarchy report" in capsys.readouterr().out
