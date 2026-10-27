import json
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.workflows.launchers import HeadlessProfile

from scripts.headless.profile_run import ProfileResult
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
    assert request.run_dir.is_relative_to(tmp_path)
