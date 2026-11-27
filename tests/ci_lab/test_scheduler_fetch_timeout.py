import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import scheduler, worktrees
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.manifests import FetchUnanswered
from scripts.ci_lab.webhooks import PLANT, HeadEvent
from tests.ci_lab import test_scheduler
from tests.ci_lab.test_scheduler import ACTOR, BASE, HEAD_A, HEAD_B, REPO, Run, plan, unit

lab = test_scheduler.lab


@pytest.fixture
def hanging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    pid = tmp_path / "remote.pid"
    helper = tmp_path / "git-remote-hanging"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os, signal\n"
        "from pathlib import Path\n"
        f"Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "signal.alarm(3)\n"
        "signal.pause()\n"
    )
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "hanging:file")
    try:
        yield "hanging::repo"
    finally:
        if pid.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid.read_text()), signal.SIGTERM)


def expander(tmp_path: Path) -> scheduler.MirrorExpander:
    return scheduler.MirrorExpander(
        tmp_path / "node",
        tmp_path / "stores",
        Toolchain("3.12", {}, "lock", "project", "worker:release", {}),
        Mock(return_value=None),
        Mock(return_value=None),
    )


@pytest.mark.feature("lab-scheduler-git-fetch-bound")
@pytest.mark.parametrize("fallback", [False, True], ids=["origin", "lab-fallback"])
def test_mirror_fetch_times_out_against_a_hanging_remote(tmp_path: Path, hanging: str, fallback: bool) -> None:
    mirror = worktrees.Mirror(tmp_path / "node", tmp_path / "store", hanging)
    if fallback:
        mirror = replace(
            mirror,
            remote=str(tmp_path / "missing.git"),
            lab=worktrees.Lab(hanging, "fixture-token"),
            repository="repo",
        )
    bounded = replace(mirror, **{"timeout_s": 0.2})
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        bounded.fetch(HEAD_A)
    assert failure.value.timeout <= 0.2
    assert failure.value.cmd == [
        "git",
        "-C",
        str(mirror.store),
        "fetch",
        "--quiet",
        "--no-write-fetch-head",
        hanging,
        f"{HEAD_A}:refs/lab/{HEAD_A}",
    ]


@pytest.mark.feature("lab-scheduler-git-fetch-bound")
@pytest.mark.parametrize("operation", ["expand", "merge_base", "trusted"])
def test_each_expander_fetch_translates_a_hanging_remote(
    tmp_path: Path, hanging: str, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(scheduler, "FETCH_TIMEOUT_S", 0.2, raising=False)
    real = subprocess.run

    def listed(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(argv, 0, f"{BASE}\trefs/heads/dev\n", "")
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", listed)
    source = expander(tmp_path)
    request = scheduler.GradeRequest(hanging, HEAD_A, BASE, scheduler.Priority.PROMOTION, ACTOR, False)
    with pytest.raises(FetchUnanswered) as failure:
        if operation == "expand":
            source.expand(request)
        elif operation == "merge_base":
            monkeypatch.setattr(source, "_dev_head", lambda repo, mirror: BASE)
            source.merge_base(hanging, HEAD_A)
        else:
            source.trusted(hanging)
    head = BASE if operation == "trusted" else HEAD_A
    assert str(failure.value) == f"git fetch of {head} from {hanging} got no answer in 0.2 seconds"
    assert isinstance(failure.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.feature("lab-scheduler-git-fetch-bound")
def test_tick_holds_a_hanging_fetch_and_continues_with_the_next_head(
    tmp_path: Path, hanging: str, monkeypatch: pytest.MonkeyPatch, lab: Callable[..., Run]
) -> None:
    monkeypatch.setattr(scheduler, "FETCH_TIMEOUT_S", 0.2, raising=False)
    run = lab({HEAD_B: plan({"lint": (unit(1),)})}, quiet_ms=500)
    original = run.lab.expander.expand
    source = expander(tmp_path)

    def expand(request: scheduler.GradeRequest) -> scheduler.Plan:
        if request.head == HEAD_A:
            return source.expand(replace(request, repo=hanging))
        return original(request)

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(HEAD_A)
    run.push(HEAD_B, lane="other")
    run.tick(1000)
    actions = run.tick(1500)
    reason = f"git fetch of {HEAD_A} from {hanging} got no answer in 0.2 seconds"
    assert f"holding {HEAD_A}: {reason}" in actions
    assert any(action.startswith("grading ") for action in actions)
    held = [json.loads(raw) for raw in run.lab.client.hvals(scheduler.WAITING)]
    assert len(held) == 1
    assert (held[0]["request"]["head"], held[0]["unanswered"], held[0]["since"]) == (HEAD_A, 1, 1500)
    assert [grade.head for grade in run.grades()] == [HEAD_B]


@pytest.mark.feature("lab-scheduler-git-fetch-bound")
def test_plant_names_a_timed_out_merge_base_and_continues_intake(
    tmp_path: Path, hanging: str, monkeypatch: pytest.MonkeyPatch, lab: Callable[..., Run]
) -> None:
    monkeypatch.setattr(scheduler, "FETCH_TIMEOUT_S", 0.2, raising=False)
    run = lab({HEAD_B: plan({"lint": (unit(1),)})}, quiet_ms=500)
    source = expander(tmp_path)
    monkeypatch.setattr(source, "_dev_head", lambda repo, mirror: BASE)

    def merge_base(repo: str, head: str) -> str:
        assert repo == REPO
        return source.merge_base(hanging, head)

    monkeypatch.setattr(run.lab.expander, "merge_base", merge_base)
    run.lab.inbox.pending.append(scheduler.Head(HeadEvent(PLANT, REPO, HEAD_A, "", ACTOR, "pushed"), HEAD_A))
    run.push(HEAD_B, lane="other")
    reason = f"git fetch of {HEAD_A} from {hanging} got no answer in 0.2 seconds"
    assert run.tick(1000) == [
        f"cannot resolve the dev merge base of plant head {HEAD_A}: {reason}",
        f"received {HEAD_B} on other",
    ]
    assert list(run.lab.client.hkeys(scheduler.WAITING)) == [f"{REPO}|other|{int(scheduler.Priority.PROMOTION)}"]
