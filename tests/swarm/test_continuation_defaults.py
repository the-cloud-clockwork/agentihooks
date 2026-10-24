import pytest

from tests.swarm.test_runtime import SEAT_TASKS, _launched


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({}, ["--model", "opus", "--effort", "medium"]),
        ({"model": "sonnet"}, ["--model", "sonnet", "--effort", "medium"]),
        ({"effort": "high"}, ["--model", "opus", "--effort", "high"]),
        ({"model": "sonnet", "effort": "high"}, ["--model", "sonnet", "--effort", "high"]),
        ({"model": "", "effort": ""}, ["--model", "opus", "--effort", "medium"]),
    ],
)
def test_continuation_completes_only_missing_run_options(tmp_path, monkeypatch, saved, expected):
    task = {
        **SEAT_TASKS["ci"],
        "handoff": "# Handoff\n<!-- handoff complete -->",
        "handoff_envelope": {"launch": {"profile": "cicd", "harness": "claude", **saved}},
    }
    assert _launched(tmp_path, monkeypatch, "ci", task) == expected


def test_continuation_uses_the_configured_lane_effort_when_the_saved_model_has_none(tmp_path, monkeypatch):
    task = {
        **SEAT_TASKS["ci"],
        "handoff": "# Handoff\n<!-- handoff complete -->",
        "handoff_envelope": {"launch": {"profile": "cicd", "harness": "claude", "model": "sonnet"}},
    }
    lanes = {"ci": {"agent": "claude", "model": "opus", "effort": "high"}}
    assert _launched(tmp_path, monkeypatch, "ci", task, lanes) == ["--model", "sonnet", "--effort", "high"]
