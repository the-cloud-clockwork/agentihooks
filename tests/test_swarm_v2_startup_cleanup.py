import argparse
import subprocess
from types import SimpleNamespace

import pytest

from tests.performance.swarm_v2 import startup

IMAGE = "kind-registry:5000/agentihooks-worker:startup"


def removal(monkeypatch, outcome):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(returncode=outcome, stdout="", stderr="DeadlineExceeded")

    monkeypatch.setattr(startup.subprocess, "run", run)
    return calls


def test_image_removal_runs_with_a_time_limit(monkeypatch):
    calls = removal(monkeypatch, 0)
    assert startup.forget_image("startup-worker", IMAGE) is True
    assert calls == [
        (
            ["docker", "exec", "startup-worker", "crictl", "--timeout", "120s", "rmi", IMAGE],
            {"capture_output": True, "text": True, "check": False, "timeout": 150},
        )
    ]


@pytest.mark.parametrize(
    "outcome, says",
    [
        (1, f"removing {IMAGE} on startup-worker failed: DeadlineExceeded"),
        (subprocess.TimeoutExpired("crictl", 150), f"removing {IMAGE} on startup-worker took over 150 seconds"),
    ],
)
def test_a_failed_image_removal_is_reported_and_never_raises(monkeypatch, capsys, outcome, says):
    removal(monkeypatch, outcome)
    assert startup.forget_image("startup-worker", IMAGE) is False
    assert capsys.readouterr().out.strip() == says


def measured(monkeypatch, tmp_path, removed, classified):
    outcome = {
        "nodes": ["n1", "n2"],
        "ready": {"uncached-n1": True, "cached-n1": True},
        "interruption": {
            "image_removed": removed,
            "classified_within_grace": classified,
            "recovered": {"interrupted-n1": True, "interrupted-n2": True},
        },
    }
    monkeypatch.setattr(startup, "scenario", lambda *args: outcome)
    monkeypatch.setattr(startup, "run", lambda *args, **kwargs: "")
    args = argparse.Namespace(image=IMAGE, nodes=["n1", "n2"], grace=120, outage=90, output=tmp_path)
    startup.kind(args)


def test_a_node_whose_image_stayed_cached_does_not_fail_the_run(monkeypatch, tmp_path):
    measured(
        monkeypatch,
        tmp_path,
        {"n1": False, "n2": True},
        {"interrupted-n1": False, "interrupted-n2": True},
    )
    assert (tmp_path / "fixture.json").exists()


def test_a_removed_image_that_never_classified_fails_the_run(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="classify its pull failure"):
        measured(
            monkeypatch,
            tmp_path,
            {"n1": True, "n2": True},
            {"interrupted-n1": True, "interrupted-n2": False},
        )
