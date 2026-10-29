import json
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest

from scripts.headless.profile_run import ProfileResult
from scripts.workflows.launchers import HeadlessProfile
from scripts.workflows.runner import StageResult
from scripts.workflows.schema import Stage

pytestmark = pytest.mark.unit


def stage():
    return Stage("build", "engineer", "low", "high", 1, ("review",), (), 1, None, {})


def test_launch_forwards_case_and_prior_outputs_to_the_full_profile(tmp_path):
    inputs = {"case": "fix the issue", "review": {"notes": ["preserve order"]}}
    result = ProfileResult("ok", {"diff": "done"}, Path("transcript"), "session", 0)
    with patch("scripts.workflows.launchers.run_profile", return_value=result) as run:
        actual = HeadlessProfile(harness="codex", timeout=17).launch(stage(), inputs, tmp_path)

    assert actual == StageResult("pass", [{"diff": "done"}])
    request = run.call_args.args[0]
    assert run.call_count == 1
    assert request.profile == "engineer"
    assert request.harness == "codex"
    assert json.loads(request.input) == inputs
    assert request.schema is None
    assert request.tier == "low"
    assert request.effort == "high"
    assert request.timeout == 17
    assert request.run_dir.relative_to(tmp_path).parts[:2] == ("outputs", "build")
    assert request.run_dir.name == "0"


def test_launch_uses_parallel_runs_and_keeps_request_order(tmp_path):
    barrier = Barrier(3)

    def execute(request):
        barrier.wait(timeout=5)
        index = int(request.run_dir.name)
        return ProfileResult("ok", index, request.run_dir / "transcript", None, 0)

    with patch("scripts.workflows.launchers.run_profile", side_effect=execute) as run:
        result = HeadlessProfile().launch(replace(stage(), count=3), {"case": "parallel"}, tmp_path)

    assert result == StageResult("pass", [0, 1, 2])
    requests = [call.args[0] for call in run.call_args_list]
    assert len(requests) == 3
    assert len({request.run_dir for request in requests}) == 3
    assert all(request.run_dir.is_relative_to(tmp_path) for request in requests)
    assert all(request.harness == "auto" and request.timeout == 900 for request in requests)
    assert all(request.profile == "engineer" for request in requests)
    assert all(request.tier == "low" and request.effort == "high" for request in requests)
    assert all(json.loads(request.input) == {"case": "parallel"} for request in requests)


@pytest.mark.parametrize("status", ["failed", "timeout", "invalid"])
def test_unsuccessful_runs_retry_and_preserve_all_outputs(tmp_path, status):
    def execute(request):
        index = int(request.run_dir.name)
        return ProfileResult("ok" if index == 0 else status, index, request.run_dir / "transcript", None, None)

    with patch("scripts.workflows.launchers.run_profile", side_effect=execute):
        result = HeadlessProfile().launch(replace(stage(), count=2), {"case": "retry"}, tmp_path)

    assert result == StageResult("retry", [0, 1])


def test_repeated_launches_preserve_existing_artifacts(tmp_path):
    def execute(request):
        request.run_dir.mkdir(parents=True)
        transcript = request.run_dir / "transcript"
        transcript.write_text("original transcript")
        return ProfileResult("ok", str(request.run_dir), transcript, None, 0)

    launcher = HeadlessProfile()
    with patch("scripts.workflows.launchers.run_profile", side_effect=execute):
        first = launcher.launch(stage(), {"case": "first"}, tmp_path)
        second = launcher.launch(stage(), {"case": "second"}, tmp_path)
        other = launcher.launch(replace(stage(), id="review"), {"case": "other"}, tmp_path)

    assert len({first.outputs[0], second.outputs[0], other.outputs[0]}) == 3
    assert all(
        (Path(result.outputs[0]) / "transcript").read_text() == "original transcript"
        for result in (first, second, other)
    )
