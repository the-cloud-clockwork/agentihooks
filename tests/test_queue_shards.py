import json

from tests import refresh_durations


def test_queue_durations_keep_the_slowest_recent_measurement_and_expire_old_runs(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    for run, seconds in [("10", 99), ("11", 1), ("12", 9), ("13", 2)]:
        for version, scale in [("3.11", 1), ("3.12", 10)]:
            folder = samples / run / f"durations-{version}-1"
            folder.mkdir(parents=True)
            (folder / "durations.json").write_text(json.dumps({"tests/t.py::test_a@serial": seconds * scale}))
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["tests/t.py::test_a"])

    refresh_durations.main(["--samples", str(samples), "--ci-run", "13", "--ci", "3"])

    assert json.loads((tmp_path / ".test_durations-slowest-3.11").read_text()) == {"tests/t.py::test_a": 9}
    assert json.loads((tmp_path / ".test_durations-slowest-3.12").read_text()) == {"tests/t.py::test_a": 90}
    assert json.loads((tmp_path / ".test_durations-slowest").read_text()) == {"tests/t.py::test_a": 90}
    assert json.loads((tmp_path / ".test_durations-3.11").read_text()) == {"tests/t.py::test_a": 2}


def test_queue_sizing_splits_a_serial_group_until_it_has_half_timeout_margin(tmp_path):
    from tests.queue_shards import matrix

    path = tmp_path / "tests/test_serial.py"
    path.parent.mkdir()
    path.write_text("import pytest\npytestmark = pytest.mark.xdist_group('serial')\n")
    measured = {f"tests/test_serial.py::test_{i}": 30.0 for i in range(48)}
    plan = matrix(tmp_path, {"3.11": measured, "3.12": measured})

    for version in ("3.11", "3.12"):
        legs = [leg for leg in plan if leg["python-version"] == version]
        assert len(legs) == 16
        assert [leg["shard"] for leg in legs] == list(range(1, 17))
        assert {leg["total"] for leg in legs} == {16}


def test_queue_sizing_keeps_independent_worker_groups_parallel(tmp_path):
    from tests.queue_shards import matrix

    for group in range(4):
        path = tmp_path / f"tests/test_group_{group}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"import pytest\npytestmark = pytest.mark.xdist_group('group_{group}')\n")
    measured = {f"tests/test_group_{group}.py::test_{i}": 30.0 for group in range(4) for i in range(12)}

    assert len(matrix(tmp_path, {"3.11": measured})) == 12


def test_a_single_test_that_cannot_fit_refuses_the_queue_plan(tmp_path):
    import pytest

    from tests.queue_shards import MAX_SHARDS, main, matrix

    path = tmp_path / "tests/test_slow.py"
    path.parent.mkdir()
    path.write_text("")

    plan = matrix(tmp_path, {"3.11": {"tests/test_slow.py::test_slow": 200.0}})
    assert len(plan) == MAX_SHARDS
    with pytest.raises(SystemExit, match="Queue plan exceeds 210s of the step timeout"):
        main(["combine", "--plans", json.dumps(plan)[1:-1], ""])


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
        "${{ fromJSON(github.event_name == 'merge_group' && format('[{0},{1}]', "
        "needs.split.outputs.queue_311, needs.split.outputs.queue_312) || '[]') }}"
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

    main(["--root", str(root), "--collection", str(collection), "--version", "3.11", "--output", str(output)])

    measured = (root / ".test_durations").read_bytes()
    assert json.loads(measured) == {"tests/test_old.py::test_old": 40.0, "tests/test_new.py::test_new": 40.0}
    assert (root / "durations.sha256").read_text().strip() == hashlib.sha256(measured).hexdigest()
    key, value = output.read_text().strip().split("=", 1)
    assert key == "queue_311"
    plan = json.loads("[" + value + "]")
    assert len(plan) == 12
    assert {leg["python-version"] for leg in plan} == {"3.11"}


def test_queue_sizing_uses_the_smallest_count_with_timeout_margin(tmp_path):
    from tests.queue_shards import matrix

    path = tmp_path / "tests/test_serial.py"
    path.parent.mkdir()
    path.write_text("import pytest\npytestmark = pytest.mark.xdist_group('serial')\n")
    measured = {f"tests/test_serial.py::test_{i}": 90.0 for i in range(13)}

    assert len(matrix(tmp_path, {"3.11": measured})) == 13


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
            "3.11",
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
        {"python-version": version, "shard": shard, "total": 20, "predicted_seconds": 150.0}
        for version in ("3.11", "3.12")
        for shard in range(1, 21)
    ]
    first = json.dumps(plan[:20])[1:-1]
    second = json.dumps(plan[20:])[1:-1]
    main(["combine", "--plans", first, second])
    assert "40 queue jobs" in capsys.readouterr().out

    second = json.dumps([*plan[20:], {**plan[-1], "shard": 21, "total": 21}])[1:-1]
    with pytest.raises(SystemExit, match="41 queue jobs exceed the pool of 40"):
        main(["combine", "--plans", first, second])


def _overflowing_plan(tmp_path, seconds):
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    for version in ("3.11", "3.12"):
        (root / f"tests/test_{version[-2:]}.py").write_text("")
        folder = tmp_path / "queue-input" / version
        folder.mkdir(parents=True)
        durations = {f"tests/test_{version[-2:]}.py::test_{i}": seconds for i in range(60)}
        (folder / ".test_durations").write_text(json.dumps(durations))
    output = tmp_path / "queue-plan.txt"
    lines = []
    for version, total in (("3.11", 16), ("3.12", 30)):
        legs = [
            {"python-version": version, "shard": s, "total": total, "predicted_seconds": 200.0}
            for s in range(1, total + 1)
        ]
        lines.append(f"queue_{version.replace('.', '')}={json.dumps(legs)[1:-1]}")
    output.write_text("\n".join(lines) + "\n")
    return root, output


def test_an_overflowing_queue_plan_is_capped_at_the_runner_pool(tmp_path, capsys):
    from tests.queue_shards import STEP_SECONDS, main

    root, output = _overflowing_plan(tmp_path, 1.0)

    main(["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")])

    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs) == {"queue_311", "queue_312"}
    plans = {key: json.loads("[" + value + "]") for key, value in outputs.items()}
    assert [leg["shard"] for leg in plans["queue_311"]] == list(range(1, 15))
    assert [leg["shard"] for leg in plans["queue_312"]] == list(range(1, 27))
    assert {leg["total"] for leg in plans["queue_311"]} == {14}
    assert {leg["total"] for leg in plans["queue_312"]} == {26}
    assert all(leg["predicted_seconds"] < STEP_SECONDS for plan in plans.values() for leg in plan)
    out = capsys.readouterr().out
    assert "46 queue jobs exceed the pool of 40; capped Python 3.11 at 14 and Python 3.12 at 26 shards" in out
    assert "40 queue jobs fit one wave of 40 runners" in out


def test_a_capped_queue_plan_that_cannot_finish_in_the_step_timeout_refuses(tmp_path):
    import pytest

    from tests.queue_shards import main

    root, output = _overflowing_plan(tmp_path, 400.0)

    with pytest.raises(SystemExit, match="cannot fit 14 queue shards below the step timeout"):
        main(["combine", "--from-output", str(output), "--root", str(root), "--inputs", str(tmp_path / "queue-input")])


def test_queue_capacity_admission_precedes_the_unit_matrix():
    from pathlib import Path

    import yaml

    jobs = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    split = jobs["split"]
    assert split["strategy"]["matrix"]["python-version"] == (
        "${{ fromJSON(github.event_name == 'merge_group' && '[\"3.11\"]' || '[\"3.11\",\"3.12\"]') }}"
    )
    step = next(step for step in split["steps"] if step.get("name") == "Size queue shards from conservative durations")
    assert step["run"].index("combine --from-output") < step["run"].index('cat queue-plan.txt >> "$GITHUB_OUTPUT"')
    assert "queue-plan" not in jobs
    assert jobs["unit"]["needs"] in (
        ["split", "select", "reuse", "admission"],
        ["split", "select", "reuse", "admission", "suite-speed"],
    )
    assert "split" in jobs["gate-required"]["needs"]


def test_joint_queue_planning_keeps_version_specific_collections_and_hashes(tmp_path):
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
    cases = {"3.11": {"tests/test_one.py::test_one": 1.0}, "3.12": {"tests/test_two.py::test_two": 2.0}}
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_one.py").write_text("")
    (tests / "test_two.py").write_text("")
    restored = tmp_path / "home/dev-durations"
    restored.mkdir(parents=True)
    merged = {node: seconds for durations in cases.values() for node, seconds in durations.items()}
    (restored / ".test_durations").write_text(json.dumps(merged))
    (tmp_path / ".test_durations").write_text(json.dumps(merged))
    for version, durations in cases.items():
        (restored / f".test_durations-{version}").write_text(json.dumps(durations))
    (tmp_path / "collected.json").write_text(json.dumps(list(cases["3.11"])))
    other = tmp_path / "queue-input/3.12"
    other.mkdir(parents=True)
    (other / "collected.json").write_text(json.dumps(list(cases["3.12"])))
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
            "PYTHON_VERSION": "3.11",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    plans = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(plans) == {"queue_311", "queue_312"}
    for version, expected in cases.items():
        snapshot = tmp_path / "queue-input" / version
        data = (snapshot / ".test_durations").read_bytes()
        assert json.loads(data) == expected
        assert (snapshot / "durations.sha256").read_text().strip() == hashlib.sha256(data).hexdigest()
    assert json.loads((tmp_path / ".test_durations").read_text()) == cases["3.11"]


def test_joint_queue_reuse_keeps_each_versions_deselection_and_uploads(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]["split"]["steps"]
    runner = tmp_path / "runner"
    (runner / "planner").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = workspace / "queue-input/3.12"
    other.mkdir(parents=True)
    common = "tests/test_common.py::test_common"
    first = "tests/test_311.py::test_one"
    second = "tests/test_312.py::test_two"
    (workspace / "reused-tests.json").write_text(json.dumps({"tests": [common, first, second]}))
    (workspace / "collected.json").write_text(json.dumps([common, first]))
    (other / "collected.json").write_text(json.dumps([common, second]))
    step = next(step for step in steps if step.get("name") == "Deselect the reused tests")
    assert step["env"]["QUEUE_BOTH_VERSIONS"] == "${{ github.event_name == 'merge_group' }}"
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_EVENT_NAME": "merge_group",
        "QUEUE_BOTH_VERSIONS": "true",
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
    assert json.loads((workspace / "reused.json").read_text())["tests"] == sorted([common, first])
    assert json.loads((other / "reused.json").read_text())["tests"] == sorted([common, second])
    uploads = {step.get("with", {}).get("name"): step for step in steps if "upload" in step.get("uses", "")}
    assert uploads["reused-3.12"]["with"]["path"] == "queue-input/3.12/reused.json"
    assert uploads["reused-tests-3.12"]["with"]["path"] == "reused-tests.json"
    for artifact in ("reused-3.12", "reused-tests-3.12"):
        assert "steps.plan.outputs.reused" in uploads[artifact]["if"]
        assert "merge_group" in uploads[artifact]["if"]
    (other / "reused.json").unlink()
    (other / "collected.json").unlink()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=workspace,
        env={**env, "QUEUE_BOTH_VERSIONS": "false"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (other / "reused.json").exists()
