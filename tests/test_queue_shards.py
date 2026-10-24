import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import refresh_durations

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _a_420_second_step(monkeypatch):
    from tests import queue_shards

    monkeypatch.setattr(queue_shards, "STEP_SECONDS", 420)


def test_the_planner_step_timeout_is_the_unit_run_tests_step_timeout():
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]["steps"]
    minutes = next(step for step in steps if step.get("name") == "Run tests")["timeout-minutes"]
    result = subprocess.run(
        [sys.executable, "-c", "from tests.queue_shards import STEP_SECONDS; print(STEP_SECONDS)"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == minutes * 60


_RECORDED_QUEUE_RUNS = [
    (347.0, 264),
    (346.9, 268),
    (346.7, 271),
    (346.7, 278),
    (347.0, 288),
    (346.7, 282),
    (346.9, 281),
    (346.8, 315),
    (346.7, 261),
    (346.5, 262),
    (346.7, 274),
    (346.4, 257),
    (346.4, 267),
    (346.5, 280),
    (342.6, 269),
    (342.6, 265),
    (342.7, 264),
    (342.6, 270),
    (342.6, 286),
    (342.6, 346),
]
_RECORDED_SLOWDOWN = 1.5


def test_the_planned_slowest_queue_shard_is_within_ten_percent_of_the_recorded_real_slowest_shard():
    from tests.queue_shards import SLOWDOWN, STARTUP_SECONDS

    ratios = sorted(
        (STARTUP_SECONDS + SLOWDOWN * (planned - STARTUP_SECONDS) / _RECORDED_SLOWDOWN) / real
        for planned, real in _RECORDED_QUEUE_RUNS
    )

    assert 0.9 <= ratios[len(ratios) // 2] <= 1.1


def test_queue_durations_keep_the_slowest_recent_measurement_and_expire_old_runs(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    for run, seconds in [("10", 99), ("11", 1), ("12", 9), ("13", 2)]:
        folder = samples / run / "durations-3.12-1"
        folder.mkdir(parents=True)
        (folder / "durations.json").write_text(json.dumps({"tests/t.py::test_a@serial": seconds}))
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["tests/t.py::test_a"])

    refresh_durations.main(["--samples", str(samples), "--ci-run", "13", "--ci", "3"])

    assert json.loads((tmp_path / ".test_durations-slowest-3.12").read_text()) == {"tests/t.py::test_a": 9}
    assert json.loads((tmp_path / ".test_durations-slowest").read_text()) == {"tests/t.py::test_a": 9}
    assert json.loads((tmp_path / ".test_durations-3.12").read_text()) == {"tests/t.py::test_a": 2}


def test_queue_sizing_splits_a_serial_group_until_it_has_half_timeout_margin(tmp_path):
    from tests.queue_shards import matrix

    path = tmp_path / "tests/test_serial.py"
    path.parent.mkdir()
    path.write_text("import pytest\npytestmark = pytest.mark.xdist_group('serial')\n")
    measured = {f"tests/test_serial.py::test_{i}": 40.0 for i in range(48)}
    plan = matrix(tmp_path, {"3.12": measured})

    legs = [leg for leg in plan if leg["python-version"] == "3.12"]
    assert len(legs) == 16
    assert [leg["shard"] for leg in legs] == list(range(1, 17))
    assert {leg["total"] for leg in legs} == {16}


@pytest.mark.parametrize(("step", "tests", "shards", "target"), [(720, 2400, 26, 195), (300, 1200, 18, 150)])
def test_queue_sizing_targets_195_seconds_or_half_a_shorter_step_timeout(
    tmp_path, monkeypatch, step, tests, shards, target
):
    from tests import queue_shards

    monkeypatch.setattr(queue_shards, "STEP_SECONDS", step)
    path = tmp_path / "tests/test_parallel.py"
    path.parent.mkdir()
    path.write_text("")
    measured = {f"tests/test_parallel.py::test_{i}": 6.0 for i in range(tests)}

    plan = queue_shards.matrix(tmp_path, {"3.12": measured})

    assert len(plan) == shards
    assert max(leg["predicted_seconds"] for leg in plan) < target


def test_queue_sizing_keeps_independent_worker_groups_parallel(tmp_path):
    from tests.queue_shards import matrix

    for group in range(4):
        path = tmp_path / f"tests/test_group_{group}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"import pytest\npytestmark = pytest.mark.xdist_group('group_{group}')\n")
    measured = {f"tests/test_group_{group}.py::test_{i}": 30.0 for group in range(4) for i in range(12)}

    assert len(matrix(tmp_path, {"3.12": measured})) == 12


def test_a_single_test_that_cannot_fit_refuses_the_queue_plan(tmp_path):
    import pytest

    from tests.queue_shards import MAX_SHARDS, main, matrix

    path = tmp_path / "tests/test_slow.py"
    path.parent.mkdir()
    path.write_text("")

    plan = matrix(tmp_path, {"3.12": {"tests/test_slow.py::test_slow": 200.0}})
    assert len(plan) == MAX_SHARDS
    with pytest.raises(SystemExit, match="Queue plan exceeds 210s of the step timeout"):
        main(["combine", "--plans", json.dumps(plan)[1:-1]])


def test_the_unit_command_uses_the_computed_queue_partition(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["unit"]["steps"] if step.get("name") == "Run tests")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").touch()
    (scripts / "ci_cpu_quota.py").write_text("print(4)\n")
    (tmp_path / "pytest.py").write_text(
        "import json\nfrom pathlib import Path\ndef main(args):\n    Path('args.json').write_text(json.dumps(args))\n    return 0\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "SHARD": "7/12", "QUEUE_SHARDS": "48", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[args.index("--shard") + 1] == "7/48"
    assert workflow["jobs"]["unit"]["strategy"]["matrix"]["include"] == (
        "${{ fromJSON(github.event_name == 'merge_group' && format('[{0}]', needs.split.outputs.queue_312) || '[]') }}"
    )


def test_queue_plan_prices_new_cases_and_publishes_one_immutable_partition(tmp_path):
    import hashlib

    from tests.queue_shards import main

    root = tmp_path / "root"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_old.py").write_text("")
    (tests / "test_new.py").write_text("")
    (root / ".test_durations").write_text(json.dumps({"tests/test_old.py::test_old": 40.0}))
    collection = root / "collected.json"
    collection.write_text(json.dumps(["tests/test_old.py::test_old", "tests/test_new.py::test_new"]))
    output = tmp_path / "output"

    main(["--root", str(root), "--collection", str(collection), "--version", "3.12", "--output", str(output)])

    measured = (root / ".test_durations").read_bytes()
    assert json.loads(measured) == {"tests/test_old.py::test_old": 40.0, "tests/test_new.py::test_new": 40.0}
    assert (root / "durations.sha256").read_text().strip() == hashlib.sha256(measured).hexdigest()
    key, value = output.read_text().strip().split("=", 1)
    assert key == "queue_312"
    plan = json.loads("[" + value + "]")
    assert len(plan) == 12
    assert {leg["python-version"] for leg in plan} == {"3.12"}


def test_queue_sizing_uses_the_smallest_count_with_timeout_margin(tmp_path):
    from tests.queue_shards import matrix

    path = tmp_path / "tests/test_serial.py"
    path.parent.mkdir()
    path.write_text("import pytest\npytestmark = pytest.mark.xdist_group('serial')\n")
    measured = {f"tests/test_serial.py::test_{i}": 90.0 for i in range(13)}

    assert len(matrix(tmp_path, {"3.12": measured})) == 13


def test_unknown_cases_use_module_maxima_and_the_global_tail_for_new_modules(tmp_path):
    from tests.queue_shards import main

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_old.py").write_text("")
    (tests / "test_new.py").write_text("")
    known = {f"tests/test_old.py::test_{i}": 0.2 for i in range(200)}
    known["tests/test_old.py::test_slow"] = 40.0
    (tmp_path / ".test_durations").write_text(json.dumps(known))
    collected = [*known, "tests/test_old.py::test_added", "tests/test_new.py::test_added"]
    collection = tmp_path / "collected.json"
    collection.write_text(json.dumps(collected))

    main(
        [
            "--root",
            str(tmp_path),
            "--collection",
            str(collection),
            "--version",
            "3.12",
            "--output",
            str(tmp_path / "out"),
        ]
    )

    measured = json.loads((tmp_path / ".test_durations").read_text())
    assert measured["tests/test_old.py::test_added"] == 40.0
    assert measured["tests/test_new.py::test_added"] == 0.2


def test_combined_queue_plan_refuses_more_jobs_than_the_runner_pool(capsys):
    import pytest

    from tests.queue_shards import main

    plan = [
        {"python-version": "3.12", "shard": shard, "total": 30, "predicted_seconds": 150.0} for shard in range(1, 31)
    ]
    main(["combine", "--plans", json.dumps(plan[:15])[1:-1], json.dumps(plan[15:])[1:-1]])
    assert "30 queue jobs fit one wave of 30 runners" in capsys.readouterr().out

    extra = {"python-version": "3.12", "shard": 31, "total": 31, "predicted_seconds": 150.0}
    with pytest.raises(SystemExit, match="31 queue jobs exceed the pool of 30"):
        main(["combine", "--plans", json.dumps(plan)[1:-1], json.dumps([extra])[1:-1]])


def _overflowing_plan(tmp_path, seconds, total=35, tests=60):
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_12.py").write_text("")
    folder = tmp_path / "queue-input" / "3.12"
    folder.mkdir(parents=True)
    durations = {f"tests/test_12.py::test_{i}": seconds for i in range(tests)}
    (folder / ".test_durations").write_text(json.dumps(durations))
    output = tmp_path / "queue-plan.txt"
    legs = [
        {"python-version": "3.12", "shard": s, "total": total, "predicted_seconds": 200.0} for s in range(1, total + 1)
    ]
    output.write_text(f"queue_312={json.dumps(legs)[1:-1]}\n")
    return root, output


def test_an_overflowing_queue_plan_is_capped_at_the_runner_pool(tmp_path, capsys):
    from tests.queue_shards import STEP_SECONDS, main

    root, output = _overflowing_plan(tmp_path, 1.0, total=35)

    main(["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")])

    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs) == {"queue_312"}
    plan = json.loads("[" + outputs["queue_312"] + "]")
    assert [leg["shard"] for leg in plan] == list(range(1, 31))
    assert {leg["total"] for leg in plan} == {30}
    assert all(leg["predicted_seconds"] < STEP_SECONDS for leg in plan)
    out = capsys.readouterr().out
    assert "35 queue jobs exceed the pool of 30; capped at 30 shards" in out
    assert "30 queue jobs fit one wave of 30 runners" in out


def test_a_forty_job_queue_plan_is_capped_to_the_thirty_runner_queue_pool(tmp_path, capsys):
    from tests.queue_shards import main

    root, output = _overflowing_plan(tmp_path, 1.0, total=40)

    main(["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")])

    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    totals = {leg["total"] for leg in json.loads("[" + outputs["queue_312"] + "]")}
    assert totals == {30}
    assert "40 queue jobs exceed the pool of 30; capped at 30 shards" in capsys.readouterr().out


def test_a_capped_queue_plan_over_the_step_timeout_warns_and_still_plans_the_pool(tmp_path, capsys):
    from tests.queue_shards import main

    root, output = _overflowing_plan(tmp_path, 400.0)

    main(["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")])

    plan = json.loads("[" + output.read_text().strip().split("=", 1)[1] + "]")
    assert len(plan) == 30
    out = capsys.readouterr().out
    assert "::warning::The capped queue plan predicts 490.0s, over 420s of the step timeout\n" in out
    assert "30 queue jobs fit one wave of 30 runners; predicted slowest test step 490.0s" in out


def test_a_capped_plan_at_the_step_timeout_warns(tmp_path, capsys, monkeypatch):
    from tests import queue_shards

    monkeypatch.setattr(queue_shards, "STEP_SECONDS", 513)
    root, output = _overflowing_plan(tmp_path, 420.0)

    queue_shards.main(
        ["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")]
    )

    assert "::warning::The capped queue plan predicts 513.0s, over 513s of the step timeout\n" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("seconds", [210.0, 500.0])
def test_an_uncapped_plan_at_half_the_step_timeout_refuses(seconds):
    from tests.queue_shards import main

    plan = [{"python-version": "3.12", "shard": 1, "total": 1, "predicted_seconds": seconds}]

    with pytest.raises(SystemExit, match=f"Queue plan exceeds 210s of the step timeout: {seconds:.1f}s"):
        main(["combine", "--plans", json.dumps(plan)[1:-1]])


def test_merge_group_runs_unit_shards_on_python_312_only():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    legs = workflow["jobs"]["unit"]["strategy"]["matrix"]
    assert legs["python-version"] == ["3.12"]
    queue_only = "${{ github.event_name == 'merge_group' && '3.12' || '' }}"
    assert {entry["python-version"] for entry in legs["exclude"]} == {queue_only}
    assert [entry["shard"] for entry in legs["exclude"]] == legs["shard"][12:]
    shard_check = workflow["jobs"]["shard-check"]["strategy"]["matrix"]["python-version"]
    assert shard_check == ["3.12"]


def test_queue_prediction_pins_the_first_shard_files_and_ignores_removed_files(tmp_path):
    from tests.queue_shards import matrix

    (tmp_path / "tests/routing").mkdir(parents=True)
    (tmp_path / "tests/routing/test_codex_api.py").write_text("")
    (tmp_path / "tests/test_a.py").write_text("")
    durations = {
        **{f"tests/routing/test_codex_api.py::test_pinned_{i}": 100.0 for i in range(8)},
        "tests/test_gone.py::test_removed": 1000.0,
        **{f"tests/test_a.py::test_{i}": 1.0 for i in range(9)},
    }

    plan = matrix(tmp_path, {"3.12": durations}, {"3.12": 2})

    assert {leg["predicted_seconds"] for leg in plan} == {261.4375}


def test_queue_capacity_admission_precedes_the_unit_matrix():
    from pathlib import Path

    import yaml

    jobs = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    split = jobs["split"]
    assert split["strategy"]["matrix"]["python-version"] == ["3.12"]
    step = next(step for step in split["steps"] if step.get("name") == "Size queue shards from conservative durations")
    assert step["run"].index("combine --from-output") < step["run"].index('cat queue-plan.txt >> "$GITHUB_OUTPUT"')
    assert "queue-plan" not in jobs
    assert jobs["unit"]["needs"] == ["split", "select", "reuse", "admission"]
    assert "split" in jobs["gate-required"]["needs"]


def test_queue_sizing_writes_the_queue_input_durations_and_hash_for_the_single_version(tmp_path):
    import hashlib
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(
        step for step in jobs["split"]["steps"] if step.get("name") == "Size queue shards from conservative durations"
    )
    case = {"tests/test_one.py::test_one": 1.0}
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_one.py").write_text("")
    restored = tmp_path / "home/dev-durations"
    restored.mkdir(parents=True)
    (restored / ".test_durations").write_text(json.dumps(case))
    (restored / ".test_durations-3.12").write_text(json.dumps(case))
    (tmp_path / ".test_durations").write_text(json.dumps(case))
    (tmp_path / "collected.json").write_text(json.dumps(list(case)))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    python = binaries / "python"
    python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "${1:-}" == -m && "${2:-}" == tests.dev_durations ]]; then\n'
        '  exec "$REAL_PYTHON" -c \'import sys; from pathlib import Path; from tests import dev_durations as d; d._ROOT = Path.cwd(); d.main(sys.argv[1:])\' "${@:3}"\n'
        "fi\n"
        'exec "$REAL_PYTHON" "$@"\n'
    )
    python.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "PYTHONPATH": str(root),
            "REAL_PYTHON": sys.executable,
            "PYTHON_VERSION": "3.12",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plans = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(plans) == {"queue_312"}
    assert json.loads("[" + plans["queue_312"] + "]")
    snapshot = tmp_path / "queue-input/3.12"
    data = (snapshot / ".test_durations").read_bytes()
    assert json.loads(data) == case
    assert (snapshot / "durations.sha256").read_text().strip() == hashlib.sha256(data).hexdigest()
    assert json.loads((tmp_path / ".test_durations").read_text()) == case


def test_the_reused_tests_step_deselects_against_the_single_collection(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]["test-count"]["steps"]
    runner = tmp_path / "runner"
    (runner / "planner").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    common = "tests/test_common.py::test_common"
    first = "tests/test_312.py::test_one"
    (workspace / "reused-tests.json").write_text(json.dumps({"tests": [common, first]}))
    (runner / "collected.json").write_text(json.dumps([common, first]))
    step = next(step for step in steps if step.get("name") == "Deselect the reused tests")
    assert "QUEUE_BOTH_VERSIONS" not in step.get("env", {})
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "PYTHON_PATH": sys.executable,
        "PYTHONPATH": str(root),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((runner / "reused.json").read_text())["tests"] == sorted([common, first])
    uploads = {step.get("with", {}).get("name"): step for step in steps if "upload" in step.get("uses", "")}
    assert uploads["reused-tests-${{ matrix.python-version }}"]["with"]["path"] == "reused-tests.json"
    assert uploads["reused-${{ matrix.python-version }}"]["with"]["path"] == "${{ runner.temp }}/reused.json"
    early = uploads["early-collected-${{ matrix.python-version }}"]["with"]["path"].splitlines()
    assert "${{ runner.temp }}/reused.json" in early
    assert "${{ runner.temp }}/plan-outputs.txt" in early
    for name in ("reused-tests-${{ matrix.python-version }}", "reused-${{ matrix.python-version }}"):
        assert "steps.plan.outputs.reused" in uploads[name]["if"]
        assert "merge_group" not in uploads[name]["if"]


def test_queue_plan_excludes_reused_work_before_matching_the_unit_split(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from tests import conftest
    from tests.queue_shards import main, matrix

    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("partial", "whole", "fresh"):
        (tests / f"test_{name}.py").write_text("")
    reused_node = "tests/test_partial.py::test_reused"
    grouped_node = reused_node + "@worker"
    whole = "tests/test_whole.py::test_old"
    remaining = {"tests/test_partial.py::test_run": 8.0, "tests/test_fresh.py::test_new": 4.0}
    measured = {**remaining, grouped_node: 9000.0, whole: 9000.0}
    snapshot = tmp_path / ".test_durations"
    snapshot.write_text(json.dumps(measured))
    collection = tmp_path / "collected.json"
    collection.write_text(json.dumps(list(measured)))
    reused = tmp_path / "reused.json"
    reused.write_text(json.dumps({"tests": [reused_node], "files": ["tests/test_whole.py"]}))
    output = tmp_path / "output"

    main(
        [
            "--root",
            str(tmp_path),
            "--collection",
            str(collection),
            "--version",
            "3.12",
            "--output",
            str(output),
            "--reused",
            str(reused),
        ]
    )

    plan = json.loads("[" + output.read_text().strip().split("=", 1)[1] + "]")
    assert len(plan) == 12
    assert json.loads(snapshot.read_text()) == remaining
    snapshot.write_text(json.dumps(measured))
    monkeypatch.setenv("CI_REUSED", str(reused))
    config = SimpleNamespace(getoption=lambda name: "1/12", stash=pytest.Stash(), rootpath=tmp_path)
    conftest._shard_files(config)
    assigned = config.stash[conftest.NODE_SHARDS]
    assert set(assigned) == set(remaining)
    assert plan == matrix(tmp_path, {"3.12": {node: measured[node] for node in assigned}})


def test_queue_sizing_consumes_reuse_after_deselection():
    from pathlib import Path

    import yaml

    steps = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())["jobs"][
        "split"
    ]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Download the early collection") < names.index("Size queue shards from conservative durations")
    assert names.index("Adopt the test plan") < names.index("Size queue shards from conservative durations")
    count = [
        step.get("name")
        for step in yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())[
            "jobs"
        ]["test-count"]["steps"]
    ]
    assert count.index("Deselect the reused tests") < count.index("Upload the early collection")
    sizing = next(step for step in steps if step.get("name") == "Size queue shards from conservative durations")
    assert "--reused" in sizing["run"]
