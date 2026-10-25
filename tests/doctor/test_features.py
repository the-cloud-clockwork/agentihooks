import json
import subprocess
import sys

import pytest

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


def test_checks_run_from_the_map_root_with_a_deadline(tmp_path, monkeypatch):
    path = _map(tmp_path / "feature-map.json", ["python", "-m", "check"])
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "first\nsecond\n", "third\n")

    monkeypatch.setattr(features.subprocess, "run", execute)
    [found] = features.findings(path)
    assert calls == [
        (
            [sys.executable, "-m", "check"],
            {
                "cwd": tmp_path,
                "capture_output": True,
                "text": True,
                "timeout": 120,
                "check": False,
            },
        )
    ]
    assert found.threshold == "a feature map live check that does not exit zero"
    assert found.measure == 1
    assert found.evidence == (
        f"operator words: {WORDS}",
        "ordered 2026-10-25 in ledger:rig-grade-swarm-doctor/tasks/t853",
        "live check python -m check exited 1",
        "output: first",
        "output: second",
        "output: third",
    )


@pytest.mark.parametrize(
    ("error", "ended"),
    [
        (subprocess.TimeoutExpired(["check"], 120), "timed out after 120 seconds"),
        (OSError("unavailable"), "could not run: OSError: unavailable"),
    ],
)
def test_unavailable_checks_preserve_the_failure_reason(tmp_path, monkeypatch, error, ended):
    def execute(*args, **kwargs):
        raise error

    monkeypatch.setattr(features.subprocess, "run", execute)
    [found] = features.findings(_map(tmp_path / "feature-map.json", ["check"]))
    assert found.evidence[-1] == f"live check check {ended}"
    assert len(found.evidence) == 3


def test_check_output_is_redacted_normalized_and_bounded(monkeypatch):
    calls = []

    def scrub(line, *, mode):
        calls.append((line, mode))
        return line.replace("private", "[redacted]")

    monkeypatch.setattr(features, "redact", scrub)
    lines = ["first", "", "two   words", "private", "four", "five", "six", "x" * 400]
    assert features._output("\n".join(lines)) == (
        "output: two words",
        "output: [redacted]",
        "output: four",
        "output: five",
        "output: six",
        "output: " + "x" * 300,
    )
    assert calls == [(line, "strict") for line in lines]
