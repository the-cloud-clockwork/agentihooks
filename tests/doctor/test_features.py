import json
import sys

from scripts.doctor import detect, features

WORDS = "All the main masters from all the swarms should run in the same tier unless the tier is exhausted."


def _map(path, *checks):
    entries = [
        {
            "id": f"feature-{n}",
            "operator_words": WORDS,
            "ordered_at": "2026-10-25",
            "ledger_item": "ledger:rig-grade-swarm-doctor/tasks/t853",
            "behaviour": "Every front door master runs on the preferred stored claude ai login.",
            "semgrep_rule": f"feature-{n}",
            "behaviour_test": "tests/test_front_door_login_check.py::test_x",
            "live_check": argv,
        }
        for n, argv in enumerate(checks, 1)
    ]
    path.write_text(json.dumps({"features": entries}))
    return path


def _exits(code, text=""):
    return ["python", "-c", f"import sys; print({text!r}); sys.exit({code})"]


def test_a_failed_live_check_is_a_regressed_feature_naming_the_operator_words(tmp_path):
    path = _map(tmp_path / "feature-map.json", _exits(0), _exits(1, "rig front door runs on a quota token"))
    [found] = features.findings(path)
    assert found.kind == "feature regressed" and found.id == "feature-regressed/feature-2"
    assert f"operator words: {WORDS}" in found.evidence
    assert "rig front door runs on a quota token" in " ".join(found.evidence)
    assert "Every front door master runs on the preferred stored claude ai login." in found.summary


def test_passing_live_checks_and_a_missing_map_report_nothing(tmp_path):
    assert features.findings(_map(tmp_path / "feature-map.json", _exits(0))) == []
    assert features.findings(tmp_path / "absent.json") == []


def test_a_live_check_that_cannot_start_is_a_regressed_feature(tmp_path):
    [found] = features.findings(_map(tmp_path / "feature-map.json", [str(tmp_path / "missing-check")]))
    assert found.subject == "feature-1"
    assert any("could not run" in line for line in found.evidence)


def test_python_live_checks_run_on_the_doctor_interpreter():
    assert features.argv(["python", "-m", "x"]) == [sys.executable, "-m", "x"]


def test_the_doctor_pass_runs_the_feature_map(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MAP", _map(tmp_path / "feature-map.json", _exits(1)))
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    [found] = detect.readers(store, None, "sw", 1, environ={}, telemetry=False)["feature"]()
    assert found.subject == "feature-1"
