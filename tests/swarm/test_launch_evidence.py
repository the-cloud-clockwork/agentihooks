import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import launch_evidence, runtime
from scripts.swarm.tick import SpawnError


@pytest.mark.parametrize("failure", ["timeout", "capacity"])
def test_failed_codex_launch_retains_redacted_evidence_before_termination(tmp_path, monkeypatch, failure):
    report = tmp_path / "validation.json"
    reason = "Selected model is at capacity" if failure == "capacity" else "no canary within 90s"
    source = {"profile": "engineer", "harness": "codex", "state": "pending", "rendered": {"canary": "expected"}}
    report.write_text(json.dumps(source))
    output = f"profile_report={report}\npane_id=pane-test\nprofile_validation=failed\n"
    credential = "AKIA" + "X" * 16
    terminated = []
    monkeypatch.setattr(runtime, "_opened_pane", lambda name, since: "pane-test")

    def run(argv, **kwargs):
        if "terminate-agent" in argv:
            records = list((tmp_path / "sw" / "launch-evidence").glob("*.json"))
            assert len(records) == 1
            text = records[0].read_text()
            assert credential not in text
            retained = json.loads(text)
            assert retained["failure"] == failure
            assert retained["validation"] == source
            assert retained["agent"] == "engineer-test"
            assert retained["task"] == "t1"
            assert retained["harness"] == "codex"
            assert retained["pane_id"] == "pane-test"
            assert "startup failed" in retained["screen"]
            assert "REDACTED" in retained["screen"]
            assert reason in retained["error"]
            report.unlink()
            terminated.append(argv)
            return SimpleNamespace(returncode=0)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 90, output=output.encode(), stderr=reason.encode())
        return SimpleNamespace(returncode=3, stdout=output, stderr=reason)

    def herdr(argv):
        assert argv == ["pane", "read", "pane-test", "--source", "recent", "--lines", "80", "--format", "text"]
        return {"text": f"startup failed: {credential}"}

    launcher = runtime.HerdrRuntime(home=tmp_path, run=run, herdr=herdr)
    config = SimpleNamespace(slug="sw", autonomy="delegate", lanes={}, compact_limit=0)
    with pytest.raises(SpawnError):
        launcher._launch(
            config, "eng", "t1", "engineer-test", ["init-agent", "--agent", "codex", "--dir", str(tmp_path)]
        )
    assert len(terminated) == 1
    assert list((tmp_path / "sw" / "launch-evidence").glob("*.json"))


@pytest.mark.parametrize("report_state", ["missing", "unreadable", "malformed"])
@pytest.mark.parametrize("pane", ["", "pane-failed"])
def test_launch_evidence_keeps_errors_when_the_report_or_screen_is_unavailable(tmp_path, report_state, pane):
    fields = {"pane_id": pane}
    if report_state != "missing":
        report = tmp_path / "report.json"
        fields["profile_report"] = str(report)
        if report_state == "malformed":
            report.write_text("{")

    def reader(pane_id, lines):
        assert (pane_id, lines) == ("pane-failed", 80)
        raise RuntimeError("pane disappeared")

    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)
    path = launch_evidence.retain(attempt, fields, "startup", "error", reader, "launch failed")
    row = json.loads(path.read_text())
    assert row["validation"]["state"] == ("missing" if report_state == "missing" else "unreadable")
    assert row["validation"]["reason"]
    assert row["screen"] == ""
    assert row["screen_error"] == ("pane disappeared" if pane else "")
    assert row["failure"] == "failed"
    assert row["stdout"] == "startup"
    assert row["stderr"] == "error"
    assert row["error"] == "launch failed\nerror"
    assert row["launched_at"] == 12
    assert path.parent == tmp_path / "launch-evidence"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("reason", ["canary timeout", "init-agent timed out", "no canary within 90s"])
def test_slow_validation_is_recorded_as_timeout_without_a_pane(tmp_path, reason):
    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)
    path = launch_evidence.retain(attempt, {}, "", "", lambda *_: pytest.fail("no pane"), reason)
    assert json.loads(path.read_text())["failure"] == "timeout"


def test_repeated_launch_failures_keep_separate_reports(tmp_path):
    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)
    first = launch_evidence.retain(attempt, {}, "", "", lambda *_: None, "first")
    second = launch_evidence.retain(attempt, {}, "", "", lambda *_: None, "second")
    assert first != second
    assert json.loads(first.read_text())["error"] == "first"
    assert json.loads(second.read_text())["error"] == "second"


@pytest.mark.parametrize("source", ["stdout", "stderr", "report", "screen"])
def test_capacity_refusal_is_distinct_even_when_the_launch_times_out(tmp_path, source):
    reason = "Selected model is at capacity"
    fields = {"pane_id": "pane"}
    report = tmp_path / "validation.json"
    report.write_text(json.dumps({"state": "failed", "reason": reason if source == "report" else ""}))
    fields["profile_report"] = str(report)
    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)
    path = launch_evidence.retain(
        attempt,
        fields,
        reason if source == "stdout" else "",
        reason if source == "stderr" else "",
        lambda *_: reason if source == "screen" else "",
        "init-agent timed out",
    )
    assert json.loads(path.read_text())["failure"] == "capacity"


def test_nested_validation_strings_are_redacted_before_json_escaping(tmp_path):
    credential = "example-" + "A" * 24
    value = "Bearer\n" + credential[:10] + "\x1b[31m" + credential[10:]
    report = tmp_path / "validation.json"
    report.write_text(json.dumps({"state": "failed", "reason": value, "evidence": [{value: [value, 7]}]}))
    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)
    path = launch_evidence.retain(attempt, {"profile_report": str(report)}, value, value, lambda *_: None, value)
    contents = path.read_text()
    assert credential not in contents
    assert credential[:10] not in contents
    row = json.loads(contents)
    assert row["validation"]["reason"] == "[REDACTED:bearer_value]"
    assert row["validation"]["evidence"] == [{"[REDACTED:bearer_value]": ["[REDACTED:bearer_value]", 7]}]
    assert row["stdout"] == row["stderr"] == "[REDACTED:bearer_value]"
    assert row["error"] == "[REDACTED:bearer_value]\n[REDACTED:bearer_value]"


def test_a_malformed_pane_response_still_leaves_diagnostics(tmp_path):
    attempt = launch_evidence.Attempt(tmp_path, "agent", "task", "codex", 12)

    def read(pane, lines):
        assert (pane, lines) == ("pane", 80)
        return {}["text"]

    path = launch_evidence.retain(attempt, {"pane_id": "pane"}, "", "", read, "timed out")
    row = json.loads(path.read_text())
    assert row["screen_error"] == "'text'"
    assert row["error"] == "timed out"
    assert row["failure"] == "timeout"
