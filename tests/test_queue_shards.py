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
    measured = {f"tests/test_serial.py::test_{i}": 30.0 for i in range(144)}
    plan = matrix(tmp_path, {"3.11": measured, "3.12": measured})

    for version in ("3.11", "3.12"):
        legs = [leg for leg in plan if leg["python-version"] == version]
        assert len(legs) == 48
        assert [leg["shard"] for leg in legs] == list(range(1, 49))
        assert {leg["total"] for leg in legs} == {48}


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

    from tests.queue_shards import matrix

    path = tmp_path / "tests/test_slow.py"
    path.parent.mkdir()
    path.write_text("")

    with pytest.raises(SystemExit, match="cannot fit below half the step timeout"):
        matrix(tmp_path, {"3.11": {"tests/test_slow.py::test_slow": 200.0}})


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
        "${{ fromJSON(needs.split.outputs.queue_matrix || '[]') }}"
    )
