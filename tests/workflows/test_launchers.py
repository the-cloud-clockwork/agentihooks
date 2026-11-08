import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from threading import Barrier
from typing import Any
from unittest.mock import Mock, patch

import pytest

from scripts.headless.profile_run import ProfileResult
from scripts.workflows.launchers import HeadlessProfile, Judge, Routed, Scout
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


def judge_stage() -> Stage:
    return Stage("verdict", "judge", "low", "medium", 1, ("scout", "build"), ("judge-blind",), 1, None, {})


def evidence_item(identifier: str) -> dict[str, str]:
    return {"id": identifier, "claim": "c", "source": "docs/a.md", "excerpt": "e", "retrieved_at": "now"}


def judged(
    returncode: int, record: dict | None = None, stdout: str = "", stderr: str = ""
) -> Callable[..., CompletedProcess]:
    def execute(command: list[str], **kwargs: Any) -> CompletedProcess:
        if record is not None:
            Path(command[command.index("--out") + 1]).write_text(json.dumps(record))
        return CompletedProcess(command, returncode, stdout, stderr)

    return execute


def test_judge_runs_the_judge_command_and_returns_its_verdict(tmp_path: Path) -> None:
    record = {"verdict": "enhance", "notes": [{"point": "p", "missing": "m"}]}
    inputs = {
        "case": '{"intent": "i", "points": ["p"]}',
        "scout": [{"items": [evidence_item("a")]}, "plain text", {"other": 1}, {"items": [evidence_item("b")]}],
        "build": [{"items": [evidence_item("c")]}],
    }
    with patch("scripts.workflows.launchers.subprocess.run", side_effect=judged(0, record)) as run:
        result = Judge(harness="codex", timeout=23).launch(judge_stage(), inputs, tmp_path)

    assert result == StageResult("enhance", record)
    command = run.call_args.args[0]
    case = Path(command[command.index("--case") + 1])
    proof = Path(command[command.index("--evidence") + 1])
    assert command == [
        "agentihooks",
        "judge",
        "--case",
        str(case),
        "--evidence",
        str(proof),
        "--tier",
        "low",
        "--harness",
        "codex",
        "--out",
        str(tmp_path / "verdict.json"),
    ]
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "timeout": 23, "check": False}
    assert case.parent == proof.parent
    assert case.parent.relative_to(tmp_path).parts[:2] == ("outputs", "verdict")
    assert case.name == "case.json"
    assert proof.name == "evidence.json"
    assert case.read_text() == inputs["case"]
    assert json.loads(proof.read_text()) == {"items": [evidence_item("a"), evidence_item("b"), evidence_item("c")]}


@pytest.mark.parametrize("returncode", [1, 2, 3])
def test_judge_failure_retries_with_the_command_output(tmp_path: Path, returncode: int) -> None:
    execute = judged(returncode, stdout="sent back", stderr="judge: refused")
    with patch("scripts.workflows.launchers.subprocess.run", side_effect=execute):
        result = Judge().launch(judge_stage(), {"case": "{}"}, tmp_path)

    assert result == StageResult("retry", {"exit": returncode, "stdout": "sent back", "stderr": "judge: refused"})
    assert not (tmp_path / "verdict.json").exists()


def test_judge_timeout_retries(tmp_path: Path) -> None:
    timeout = TimeoutExpired(["agentihooks", "judge"], 900)
    with patch("scripts.workflows.launchers.subprocess.run", side_effect=timeout) as run:
        result = Judge().launch(judge_stage(), {"case": "{}"}, tmp_path)

    assert result == StageResult("retry", {"exit": None, "stdout": "", "stderr": "judge timed out after 900 seconds"})
    command = run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == 900
    assert command[command.index("--harness") + 1] == "auto"


def test_routed_sends_filtered_stages_to_the_judge_only(tmp_path: Path) -> None:
    profile = Mock(launch=Mock(return_value=StageResult("pass", ["built"])))
    judge = Mock(launch=Mock(return_value=StageResult("retry", {"verdict": "retry"})))
    routed = Routed(profile, judge)

    assert routed.launch(judge_stage(), {"case": "a"}, tmp_path) == StageResult("retry", {"verdict": "retry"})
    assert routed.launch(stage(), {"case": "b"}, tmp_path) == StageResult("pass", ["built"])

    judge.launch.assert_called_once_with(judge_stage(), {"case": "a"}, tmp_path)
    profile.launch.assert_called_once_with(stage(), {"case": "b"}, tmp_path)


def test_headless_profile_refuses_a_filtered_stage(tmp_path: Path) -> None:
    with patch("scripts.workflows.launchers.run_profile") as run, pytest.raises(ValueError) as refused:
        HeadlessProfile().launch(judge_stage(), {"case": "a"}, tmp_path)

    assert str(refused.value) == "verdict carries filters and runs only through agentihooks judge"
    run.assert_not_called()
    assert not (tmp_path / "outputs").exists()


def scout_stage() -> Stage:
    return Stage("scout", "scouter", "high", "medium", 1, ("case",), (), 2, "every point has evidence", {})


def evidence(folder: Path) -> dict:
    return {"items": [{"id": "e1", "claim": folder.name, "source": "s", "excerpt": "x", "retrieved_at": "t"}]}


def writes_evidence(argv: list[str], env: dict, run_dir: Path, timeout: float) -> int:
    Path(argv[argv.index("--out") + 1]).write_text(json.dumps(evidence(run_dir)))
    return 0


def test_scout_runs_the_headless_scout_command_with_the_stage_settings(tmp_path: Path) -> None:
    inputs = {"case": "what changed", "plan": ["read the code"]}
    with patch("scripts.workflows.launchers.execute", side_effect=writes_evidence) as run:
        result = Scout(harness="codex", timeout=23).launch(scout_stage(), inputs, tmp_path)

    argv, env, run_dir, timeout = run.call_args.args
    assert run.call_count == 1
    assert run_dir.relative_to(tmp_path).parts[:2] == ("outputs", "scout")
    assert run_dir.name == "0"
    assert run_dir.is_dir()
    assert argv == [
        "agentihooks",
        "scout",
        json.dumps(inputs),
        "--rounds",
        "2",
        "--tier",
        "high",
        "--harness",
        "codex",
        "--out",
        str(run_dir / "evidence.json"),
        "--until",
        "every point has evidence",
    ]
    assert env == dict(os.environ)
    assert timeout == 23
    assert result == StageResult("pass", [evidence(run_dir)])


def test_scout_without_an_exit_scouts_every_round(tmp_path: Path) -> None:
    with patch("scripts.workflows.launchers.execute", side_effect=writes_evidence) as run:
        Scout().launch(replace(scout_stage(), exit=None, tier="low"), {"case": "c"}, tmp_path)

    argv, _, run_dir, timeout = run.call_args.args
    assert argv[3:] == ["--rounds", "2", "--tier", "low", "--harness", "auto", "--out", str(run_dir / "evidence.json")]
    assert timeout == 900


def test_scout_count_runs_in_parallel_and_keeps_order(tmp_path: Path) -> None:
    barrier = Barrier(3)

    def execute(argv: list[str], env: dict, run_dir: Path, timeout: float) -> int:
        barrier.wait(timeout=5)
        return writes_evidence(argv, env, run_dir, timeout)

    with (
        patch("scripts.workflows.launchers.execute", side_effect=execute),
        patch("scripts.workflows.launchers.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as pool,
    ):
        result = Scout().launch(replace(scout_stage(), count=3), {"case": "c"}, tmp_path)

    assert pool.call_args.kwargs == {"max_workers": 3}
    assert result.outcome == "pass"
    assert [output["items"][0]["claim"] for output in result.outputs] == ["0", "1", "2"]


@pytest.mark.parametrize("code", [1, None])
def test_a_failed_scout_retries_and_keeps_the_other_evidence(tmp_path: Path, code: int | None) -> None:
    def execute(argv: list[str], env: dict, run_dir: Path, timeout: float) -> int | None:
        writes_evidence(argv, env, run_dir, timeout)
        return 0 if run_dir.name == "0" else code

    with patch("scripts.workflows.launchers.execute", side_effect=execute):
        result = Scout().launch(replace(scout_stage(), count=2), {"case": "c"}, tmp_path)

    assert result.outcome == "retry"
    assert result.outputs[0]["items"][0]["claim"] == "0"
    assert result.outputs[1] is None


def test_a_scout_that_writes_no_evidence_retries(tmp_path: Path) -> None:
    with patch("scripts.workflows.launchers.execute", return_value=0):
        result = Scout().launch(scout_stage(), {"case": "c"}, tmp_path)

    assert result == StageResult("retry", [None])


def test_repeated_scout_launches_keep_earlier_evidence(tmp_path: Path) -> None:
    with patch("scripts.workflows.launchers.execute", side_effect=writes_evidence) as run:
        Scout().launch(scout_stage(), {"case": "first"}, tmp_path)
        Scout().launch(scout_stage(), {"case": "second"}, tmp_path)

    folders = [call.args[2] for call in run.call_args_list]
    assert folders[0] != folders[1]
    assert all((folder / "evidence.json").is_file() for folder in folders)


def test_the_profile_launcher_sends_scouter_stages_to_the_scout(tmp_path: Path) -> None:
    with (
        patch("scripts.workflows.launchers.execute", side_effect=writes_evidence) as run,
        patch("scripts.workflows.launchers.run_profile") as profile,
    ):
        result = HeadlessProfile(harness="claude", timeout=31).launch(scout_stage(), {"case": "c"}, tmp_path)

    argv, _, run_dir, timeout = run.call_args.args
    assert profile.call_count == 0
    assert argv[2] == json.dumps({"case": "c"})
    assert argv[argv.index("--harness") + 1] == "claude"
    assert timeout == 31
    assert result == StageResult("pass", [evidence(run_dir)])


def test_the_profile_launcher_runs_other_stages_as_profiles(tmp_path: Path) -> None:
    result = ProfileResult("ok", "built", Path("transcript"), None, 0)
    with (
        patch("scripts.workflows.launchers.execute") as run,
        patch("scripts.workflows.launchers.run_profile", return_value=result),
    ):
        actual = HeadlessProfile().launch(stage(), {"case": "c"}, tmp_path)

    assert run.call_count == 0
    assert actual == StageResult("pass", ["built"])
