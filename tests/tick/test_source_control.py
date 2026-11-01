import json
import subprocess
from pathlib import Path

import pytest

from scripts.tick.adapters import fakes, github
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Unsupported
from scripts.tick.ports import source_control as port

REFERENCE = port.ChangeRef("owner/project", 7)
PULL = {
    "number": 7,
    "state": "open",
    "head": {"sha": "head", "ref": "engineer-1"},
    "base": {"ref": "dev"},
    "merged": False,
    "html_url": "https://github.com/owner/project/pull/7",
    "body": "Closes #12",
    "draft": True,
}
OPTIONS = {"capture_output": True, "text": True, "check": True}


class Recorder:
    def __init__(self, answers: list[object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, list[str], dict[str, object]]] = []

    def __call__(self, caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((caller, args, options))
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(self.answers.pop(0)), "")


def _provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[object]
) -> tuple[github.GitHubSourceControl, Recorder]:
    recorder = Recorder(answers)
    monkeypatch.setattr(github.github_reads, "rest", recorder)
    return github.GitHubSourceControl("owner/project", tmp_path, "dev"), recorder


def _deadline() -> Deadline:
    return Deadline.after(9, clock=lambda: 0.0)


@pytest.mark.parametrize("operation", ["enqueue", "queue_health"])
def test_fake_without_queue_reports_unsupported(operation: str) -> None:
    provider = fakes.FakeSourceControl()
    deadline = Deadline.after(10, clock=lambda: 0.0)
    method = getattr(provider, operation)
    args = (REFERENCE,) if operation == "enqueue" else ()

    assert method(*args, deadline=deadline) == Unsupported(operation)
    assert provider.calls[-1].operation == operation
    assert provider.calls[-1].args == args
    assert provider.calls[-1].deadline is deadline
    assert provider.capabilities(deadline=deadline) == port.Capabilities(merge_queue=False)


def test_fake_with_queue_returns_supplied_answers() -> None:
    entry = port.QueueEntry(REFERENCE, 2)
    health = port.QueueHealth(1, ("QUEUED",))
    provider = fakes.FakeSourceControl({"enqueue": entry, "queue_health": health}, merge_queue=True)
    deadline = Deadline.after(10, clock=lambda: 0.0)

    assert provider.enqueue(REFERENCE, deadline=deadline) is entry
    assert provider.queue_health(deadline=deadline) is health
    assert provider.capabilities(deadline=deadline) == port.Capabilities(merge_queue=True)
    queued = fakes.FakeSourceControl(merge_queue=True)
    assert queued.enqueue(REFERENCE, deadline=deadline) == port.QueueEntry(REFERENCE, 1)
    assert queued.queue_health(deadline=deadline) == port.QueueHealth(0, ())


def test_fake_without_queue_returns_supplied_answers() -> None:
    entry = port.QueueEntry(REFERENCE, 4)
    provider = fakes.FakeSourceControl({"enqueue": entry})
    deadline = Deadline.after(10, clock=lambda: 0.0)

    assert provider.enqueue(REFERENCE, deadline=deadline) is entry
    assert provider.queue_health(deadline=deadline) == Unsupported("queue_health")


@pytest.mark.parametrize(
    "operation,args,answer",
    [
        ("change", (REFERENCE,), port.ChangeState(REFERENCE, "open", "head", "dev", False, "https://x/7")),
        ("merge", (REFERENCE,), port.Merged(REFERENCE, "sha")),
        ("comment", (REFERENCE, "body"), port.Comment(REFERENCE, "https://x/c")),
    ],
)
def test_fake_change_merge_and_comment_record_and_answer(
    operation: str, args: tuple[object, ...], answer: object
) -> None:
    provider = fakes.FakeSourceControl({operation: answer})
    deadline = Deadline.after(10, clock=lambda: 0.0)

    assert getattr(provider, operation)(*args, deadline=deadline) is answer
    assert provider.calls == [fakes.Call(operation, args, deadline)]
    with pytest.raises(TimeoutError):
        getattr(provider, operation)(*args, deadline=Deadline(0, clock=lambda: 0.0))
    assert len(provider.calls) == 1


def test_fake_reads_default_to_empty() -> None:
    provider = fakes.FakeSourceControl()
    deadline = Deadline.after(10, clock=lambda: 0.0)

    assert provider.checks(REFERENCE, deadline=deadline) == ()
    assert provider.runs(REFERENCE, deadline=deadline) == ()
    assert provider.tags(deadline=deadline) == ()
    assert [call.operation for call in provider.calls] == ["checks", "runs", "tags"]


def test_fake_refuses_expired_deadline() -> None:
    provider = fakes.FakeSourceControl()

    with pytest.raises(TimeoutError):
        provider.enqueue(REFERENCE, deadline=Deadline(0, clock=lambda: 0.0))
    assert provider.calls == []


def test_domain_types_preserve_provider_neutral_state() -> None:
    change = port.ChangeState(REFERENCE, "open", "head", "dev", False, "https://example.test/7")
    check = port.CheckState("lint", "completed", "success", "https://example.test/check/1")
    run = port.Run(19, "Tests", "head", "completed", "success", "https://example.test/run/19")

    assert change.reference == REFERENCE
    assert (change.state, change.head, change.base, change.merged, change.url) == (
        "open",
        "head",
        "dev",
        False,
        "https://example.test/7",
    )
    assert (check.name, check.status, check.conclusion, check.url) == (
        "lint",
        "completed",
        "success",
        "https://example.test/check/1",
    )
    assert (run.id, run.name, run.head, run.status, run.conclusion, run.url) == (
        19,
        "Tests",
        "head",
        "completed",
        "success",
        "https://example.test/run/19",
    )


@pytest.mark.feature("swarm-tick-source-control-port")
def test_github_change_uses_audited_reads_and_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL])

    assert provider.change(REFERENCE, deadline=_deadline()) == port.ChangeState(
        REFERENCE,
        "open",
        "head",
        "dev",
        False,
        "https://github.com/owner/project/pull/7",
        "engineer-1",
        "Closes #12",
        True,
    )
    assert recorder.calls == [
        ("tick.source_control", ["api", "repos/owner/project/pulls/7"], {"cwd": tmp_path, "timeout": 9, **OPTIONS})
    ]


@pytest.mark.feature("swarm-tick-source-control-port")
def test_github_change_names_its_head_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pull = {**PULL, "head": {"sha": "head", "ref": "engineer-2"}}
    provider, _ = _provider(monkeypatch, tmp_path, [pull])

    assert provider.change(REFERENCE, deadline=_deadline()).head_branch == "engineer-2"


@pytest.mark.feature("swarm-tick-source-control-port")
def test_github_change_reads_an_empty_body_as_no_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, _ = _provider(monkeypatch, tmp_path, [{**PULL, "body": None, "draft": False}])

    change = provider.change(REFERENCE, deadline=_deadline())
    assert (change.body, change.draft) == ("", False)


@pytest.mark.feature("swarm-tick-source-control-port")
def test_a_change_record_defaults_to_no_body_and_not_a_draft() -> None:
    change = port.ChangeState(REFERENCE, "open", "head", "dev", False, "https://x/7")
    assert (change.head_branch, change.body, change.draft) == ("", "", False)


@pytest.mark.feature("swarm-tick-source-control-port")
def test_github_command_is_the_one_gh_command_line() -> None:
    assert github.command(["pr", "view", "7"]) == ["gh", "pr", "view", "7"]
    assert github.command([]) == ["gh"]


def test_github_checks_read_the_current_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runs = {
        "check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success", "html_url": "https://c/1"},
            {"name": "tests", "status": "in_progress", "conclusion": None, "html_url": "https://c/2"},
        ]
    }
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL, runs])

    assert provider.checks(REFERENCE, deadline=_deadline()) == (
        port.CheckState("lint", "completed", "success", "https://c/1"),
        port.CheckState("tests", "in_progress", None, "https://c/2"),
    )
    assert recorder.calls[1][1] == ["api", "repos/owner/project/commits/head/check-runs?per_page=100&page=1"]


def test_github_runs_read_the_current_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {
        "id": 19,
        "name": "Tests",
        "head_sha": "head",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://r/19",
    }
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL, {"workflow_runs": [row]}])

    assert provider.runs(REFERENCE, deadline=_deadline()) == (
        port.Run(19, "Tests", "head", "completed", "failure", "https://r/19"),
    )
    assert recorder.calls[1][1] == ["api", "repos/owner/project/actions/runs?head_sha=head&per_page=100&page=1"]


def test_github_checks_follow_full_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {"name": "lint", "status": "completed", "conclusion": "success", "html_url": "https://c/1"}
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL, {"check_runs": [row] * 100}, {"check_runs": [row]}])

    assert len(provider.checks(REFERENCE, deadline=_deadline())) == 101
    assert [call[1][1] for call in recorder.calls[1:]] == [
        "repos/owner/project/commits/head/check-runs?per_page=100&page=1",
        "repos/owner/project/commits/head/check-runs?per_page=100&page=2",
    ]


def test_github_runs_stop_at_the_page_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {"id": 1, "name": "T", "head_sha": "head", "status": "s", "conclusion": None, "html_url": "u"}
    pages: list[object] = [{"workflow_runs": [row] * 100} for _ in range(10)]
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL, *pages])

    assert len(provider.runs(REFERENCE, deadline=_deadline())) == 1000
    assert len(recorder.calls) == 11
    assert recorder.calls[-1][1][1].endswith("&page=10")


def test_github_enqueue_pins_the_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pull = {"data": {"repository": {"mergeQueue": {"id": "Q"}, "pullRequest": {"id": "P", "headRefOid": "head"}}}}
    queued = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 3}}}}
    provider, recorder = _provider(monkeypatch, tmp_path, [pull, queued])

    assert provider.enqueue(REFERENCE, deadline=_deadline()) == port.QueueEntry(REFERENCE, 3)
    read, write = recorder.calls[0][1], recorder.calls[1][1]
    assert read[:4] == ["api", "graphql", "-f", f"query={github._PULL}"]
    assert read[4:] == ["-f", "owner=owner", "-f", "name=project", "-F", "number=7", "-f", "base=dev"]
    assert write[4:] == ["-f", "id=P", "-f", "head=head"]
    assert write[3] == f"query={github._ENQUEUE}"


def test_github_enqueue_without_queue_is_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pull = {"data": {"repository": {"mergeQueue": None, "pullRequest": {"id": "P", "headRefOid": "head"}}}}
    provider, recorder = _provider(monkeypatch, tmp_path, [pull])

    assert provider.enqueue(REFERENCE, deadline=_deadline()) == Unsupported("enqueue")
    assert len(recorder.calls) == 1


def test_github_merge_squashes_the_read_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [PULL, {"sha": "merged", "merged": True}])

    assert provider.merge(REFERENCE, deadline=_deadline()) == port.Merged(REFERENCE, "merged")
    assert recorder.calls[1][1] == [
        "api",
        "repos/owner/project/pulls/7/merge",
        "-X",
        "PUT",
        "-f",
        "sha=head",
        "-f",
        "merge_method=squash",
    ]


def test_github_queue_health_and_capabilities(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    entries = {"totalCount": 2, "nodes": [{"state": "QUEUED"}, {"state": "MERGEABLE"}]}
    queue = {"data": {"repository": {"mergeQueue": {"entries": entries}}}}
    provider, recorder = _provider(monkeypatch, tmp_path, [queue, queue])

    assert provider.queue_health(deadline=_deadline()) == port.QueueHealth(2, ("QUEUED", "MERGEABLE"))
    assert provider.capabilities(deadline=_deadline()) == port.Capabilities(merge_queue=True)
    assert recorder.calls[0][1][3] == f"query={github._QUEUE}"
    assert recorder.calls[0][1][4:] == ["-f", "owner=owner", "-f", "name=project", "-f", "base=dev"]


def test_github_without_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = {"data": {"repository": {"mergeQueue": None}}}
    provider, _ = _provider(monkeypatch, tmp_path, [empty, empty])

    assert provider.queue_health(deadline=_deadline()) == Unsupported("queue_health")
    assert provider.capabilities(deadline=_deadline()) == port.Capabilities(merge_queue=False)


def test_github_comment_posts_on_the_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [{"html_url": "https://c/9"}])

    assert provider.comment(REFERENCE, "hello", deadline=_deadline()) == port.Comment(REFERENCE, "https://c/9")
    assert recorder.calls[0][1] == ["api", "repos/owner/project/issues/7/comments", "-f", "body=hello"]


def test_github_issue_state_reads_the_issue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [{"state": "open"}])

    assert provider.issue_state(REFERENCE, deadline=_deadline()) == "open"
    assert recorder.calls == [
        (github.CALLER, ["api", "repos/owner/project/issues/7"], {"cwd": tmp_path, "timeout": 9, **OPTIONS})
    ]


def test_github_close_issue_patches_its_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [{"state": "closed"}])

    assert provider.close_issue(REFERENCE, deadline=_deadline()) == "closed"
    assert recorder.calls[0][1] == ["api", "repos/owner/project/issues/7", "-X", "PATCH", "-f", "state=closed"]


def test_github_rate_limit_reads_the_core_quota(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    core = {"used": 40, "remaining": 4960, "reset": 1700}
    provider, recorder = _provider(monkeypatch, tmp_path, [{"resources": {"core": core, "search": {}}}])

    assert provider.rate_limit(deadline=_deadline()) == port.RateLimit(40, 4960, 1700)
    assert recorder.calls[0][1] == ["api", "rate_limit"]


@pytest.mark.feature("swarm-tick-source-control-port")
@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            [{"merged_at": None}, {"merged_at": "2026-10-19T21:20:03Z"}, {"merged_at": "2026-10-20T00:00:00Z"}],
            "2026-10-19T21:20:03Z",
        ),
        ([{"merged_at": None}], None),
        ([], None),
    ],
)
def test_github_merged_at_reads_the_first_merged_change_carrying_a_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: list[dict[str, str | None]], expected: str | None
) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [rows])

    assert provider.merged_at("abc123", deadline=_deadline()) == expected
    assert recorder.calls == [
        (github.CALLER, ["api", "repos/owner/project/commits/abc123/pulls"], {"cwd": tmp_path, "timeout": 9, **OPTIONS})
    ]


@pytest.mark.parametrize("enabled", [True, False])
def test_github_issues_enabled_reads_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    provider, recorder = _provider(monkeypatch, tmp_path, [{"has_issues": enabled, "has_wiki": not enabled}])

    assert provider.issues_enabled(deadline=_deadline()) is enabled
    assert recorder.calls == [
        (github.CALLER, ["api", "repos/owner/project"], {"cwd": tmp_path, "timeout": 9, **OPTIONS})
    ]


def test_github_latest_runs_read_the_newest_runs_of_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = [
        {"id": 5, "head_branch": "feature", "event": "pull_request", "status": "queued", "name": "Tests"},
        {"id": 6, "head_branch": "dev", "event": "push", "status": "completed", "name": "Lint"},
        {"id": 7, "head_branch": None, "event": "schedule", "status": "in_progress", "name": "Nightly"},
    ]
    provider, recorder = _provider(monkeypatch, tmp_path, [{"total_count": 3, "workflow_runs": rows}])

    assert provider.latest_runs(deadline=_deadline()) == (
        port.RepositoryRun(5, "feature", "pull_request", "queued"),
        port.RepositoryRun(6, "dev", "push", "completed"),
        port.RepositoryRun(7, None, "schedule", "in_progress"),
    )
    assert recorder.calls == [
        (
            github.CALLER,
            ["api", "repos/owner/project/actions/runs?per_page=100"],
            {"cwd": tmp_path, "timeout": 9, **OPTIONS},
        )
    ]


@pytest.mark.parametrize("body", ["", "{}"])
def test_github_cancel_run_posts_the_cancel_without_reading_a_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    seen: list[tuple[str, list[str], dict[str, object]]] = []

    def rest(caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        seen.append((caller, args, options))
        return subprocess.CompletedProcess(["gh", *args], 0, body, "")

    monkeypatch.setattr(github.github_reads, "rest", rest)
    provider = github.GitHubSourceControl("owner/project", tmp_path, "dev")

    assert provider.cancel_run(5, deadline=_deadline()) is None
    assert seen == [
        (
            github.CALLER,
            ["api", "repos/owner/project/actions/runs/5/cancel", "-X", "POST"],
            {"cwd": tmp_path, "timeout": 9, **OPTIONS},
        )
    ]


@pytest.mark.parametrize(
    "operation,args,answer",
    [
        ("issue_state", (REFERENCE,), "open"),
        ("close_issue", (REFERENCE,), "closed"),
        ("rate_limit", (), port.RateLimit(1, 2, 3)),
        ("issues_enabled", (), True),
        ("latest_runs", (), (port.RepositoryRun(1, "feature", "push", "queued"),)),
        ("cancel_run", (5,), None),
        ("merged_at", ("abc123",), "2026-10-19T21:20:03Z"),
    ],
)
def test_fake_issue_and_rate_limit_tools_record_and_answer(
    operation: str, args: tuple[object, ...], answer: object
) -> None:
    provider = fakes.FakeSourceControl({operation: answer})
    deadline = Deadline.after(10, clock=lambda: 0.0)

    assert getattr(provider, operation)(*args, deadline=deadline) is answer
    assert provider.calls == [fakes.Call(operation, args, deadline)]


def test_github_tags_read_the_git_remote(tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def git(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        seen.append((command, options))
        output = "a1\trefs/tags/v1.0.0\nb2\trefs/tags/v1.0.0^{}\n\nc3\trefs/tags/v1.1.0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    provider = github.GitHubSourceControl("owner/project", tmp_path, "dev", git=git)

    assert provider.tags(deadline=_deadline()) == (port.Tag("v1.0.0", "a1"), port.Tag("v1.1.0", "c3"))
    assert seen == [(["git", "ls-remote", "--tags", "origin"], {"cwd": tmp_path, "timeout": 9, **OPTIONS})]


def test_github_defaults() -> None:
    provider = github.GitHubSourceControl("owner/project", Path("."), "main")

    assert provider.base == "main"
    assert provider.git is subprocess.run
    assert github.CALLER == "tick.source_control"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("change", (REFERENCE,)),
        ("checks", (REFERENCE,)),
        ("runs", (REFERENCE,)),
        ("enqueue", (REFERENCE,)),
        ("merge", (REFERENCE,)),
        ("queue_health", ()),
        ("tags", ()),
        ("comment", (REFERENCE, "body")),
        ("capabilities", ()),
        ("issue_state", (REFERENCE,)),
        ("close_issue", (REFERENCE,)),
        ("rate_limit", ()),
        ("issues_enabled", ()),
        ("latest_runs", ()),
        ("cancel_run", (5,)),
        ("merged_at", ("abc123",)),
    ],
)
def test_github_expired_deadline_prevents_external_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str, args: tuple[object, ...]
) -> None:
    def refuse(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("expired deadline reached the provider")

    monkeypatch.setattr(github.github_reads, "rest", refuse)
    provider = github.GitHubSourceControl("owner/project", tmp_path, "dev", git=refuse)

    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        getattr(provider, operation)(*args, deadline=Deadline(0, clock=lambda: 0.0))


def test_the_feature_map_names_the_source_control_port_seam() -> None:
    root = Path(__file__).resolve().parents[2]
    entries = {entry["id"]: entry for entry in json.loads((root / "feature-map.json").read_text())["features"]}
    entry = entries["swarm-tick-source-control-port"]
    assert entry["seam"] == "The source control port in scripts.tick.ports.source_control and its GitHub adapter"
    assert all(test.startswith("tests/tick/test_source_control.py::") for test in entry["tests"])
