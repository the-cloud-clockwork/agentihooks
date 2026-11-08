import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm import workflow_job


@pytest.mark.feature("swarm-workflow-runner")
def test_background_job_holds_workspace_lock_until_child_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[int] = []

    def launch(argv: list[str], **kwargs: object) -> None:
        held.append(os.dup(kwargs["pass_fds"][0]))
        assert argv[:3] == [workflow_job.sys.executable, "-m", "scripts.swarm.workflow_job"]
        assert argv[3:] == [str(kwargs["pass_fds"][0]), str(tmp_path / "workflow-task.json")]
        assert kwargs["stdin"] == -3
        assert kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["start_new_session"] is True
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == Path(workflow_job.__file__).resolve().parents[2]

    monkeypatch.setattr(workflow_job, "subprocess", SimpleNamespace(Popen=Mock(side_effect=launch), DEVNULL=-3))
    request = {"task": {"workspace": str(tmp_path), "id": "work"}, "repo": str(tmp_path)}
    try:
        assert workflow_job.start(request) is True
        assert workflow_job.start(request) is False
        assert workflow_job.subprocess.Popen.call_count == 1
        assert json.loads((tmp_path / "workflow-task.json").read_text()) == request
    finally:
        for fd in held:
            os.close(fd)
    held.clear()
    try:
        assert workflow_job.start(request) is True
        assert workflow_job.subprocess.Popen.call_count == 2
    finally:
        for fd in held:
            os.close(fd)


@pytest.mark.feature("swarm-workflow-runner")
def test_failed_spawn_releases_workspace_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow_job, "subprocess", SimpleNamespace(Popen=Mock(side_effect=OSError("launch failed")), DEVNULL=-3)
    )
    request = {"task": {"workspace": str(tmp_path)}, "repo": str(tmp_path)}
    with pytest.raises(OSError, match="launch failed"):
        workflow_job.start(request)
    with (tmp_path / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.feature("swarm-workflow-runner")
def test_worker_advances_once_and_releases_lock_on_success_or_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fails: bool
) -> None:
    task = {"workspace": str(tmp_path), "id": "work"}
    payload = tmp_path / "workflow-task.json"
    payload.write_text(json.dumps({"task": task, "repo": str(tmp_path)}))
    inbox = object()
    monkeypatch.setattr(workflow_job, "connect", Mock(return_value=inbox))
    advance = Mock(side_effect=ValueError("failed") if fails else None, return_value="workflow advanced")
    monkeypatch.setattr(workflow_job.workflow_runner, "advance_task", advance)
    with (tmp_path / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.dup(lock.fileno())
    if fails:
        with pytest.raises(ValueError, match="failed"):
            workflow_job.main([str(fd), str(payload)])
    else:
        assert workflow_job.main([str(fd), str(payload)]) == 0
        assert capsys.readouterr().out == "workflow advanced\n"
    advance.assert_called_once_with(inbox, task, tmp_path)
    with pytest.raises(OSError):
        os.fstat(fd)
    with (tmp_path / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
