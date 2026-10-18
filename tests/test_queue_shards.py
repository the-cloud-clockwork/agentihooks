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
