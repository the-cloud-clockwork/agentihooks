from collections.abc import Callable
from typing import Any

import pytest

from scripts.gates import progress
from scripts.swarm.keyspace import ROOT as KEY_ROOT

SLUG, ME = "demo", "engineer@abcdef-0001"


@pytest.fixture
def store():
    import fakeredis

    return progress.Progress(fakeredis.FakeRedis(decode_responses=True), SLUG)


def test_an_agent_with_no_record_reads_empty(store):
    assert store.read(ME) == progress.Mark(outcome_at=0, outcome="", talk=0)


def test_talk_counts_up_from_one(store):
    assert [store.talk(ME), store.talk(ME), store.talk(ME)] == [1, 2, 3]
    assert store.read(ME).talk == 3


def test_an_outcome_stamps_its_kind_and_zeroes_the_talk(store):
    store.talk(ME)
    store.talk(ME)
    store.outcome(ME, "pushed", now_ms=1234)
    assert store.read(ME) == progress.Mark(outcome_at=1234, outcome="pushed", talk=0)
    assert store.talk(ME) == 1


def test_agents_and_swarms_keep_separate_marks(store):
    store.talk(ME)
    assert store.read("engineer@abcdef-0002").talk == 0
    assert progress.Progress(store.redis, "elsewhere").read(ME).talk == 0
    assert store.key(ME) == f"{KEY_ROOT}:swarm:{SLUG}:progress:{ME}"


def test_outcome_once_stamps_a_resolution_only_the_first_time(store):
    assert store.outcome_once(ME, "checks resolved", "pr-1 pass", now_ms=10) is True
    store.talk(ME)
    assert store.outcome_once(ME, "checks resolved", "pr-1 pass", now_ms=20) is False
    assert store.read(ME) == progress.Mark(outcome_at=10, outcome="checks resolved", talk=1)
    assert store.outcome_once(ME, "checks resolved", "pr-1 fail", now_ms=30) is True
    assert store.read(ME) == progress.Mark(outcome_at=30, outcome="checks resolved", talk=0)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin engineer-323133-0115",
        "cd /w && git push origin HEAD",
        "gh pr create --base dev --title x --body-file b.md",
        "git -C /w push",
        "cd /w\ngit push -u origin HEAD",
        "cd /w\ngh pr create --base dev --fill",
    ],
)
def test_pushes_and_opened_pull_requests_are_outcomes(command):
    assert progress.outcome_of(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commit -m push",
        "echo git push",
        "gh pr view 3",
        "gh pr checks 3",
        "grep 'gh pr create' notes.md",
        "",
    ],
)
def test_other_commands_are_not_outcomes(command):
    assert progress.outcome_of(command) == ""


URL = "https://github.com/o/r/pull/7"


def pull(resolved: bool = True, red: bool = False, pushed_at: int = 100, head: str = "") -> Any:
    from scripts.swarm.ledger_events import PullRequest

    return PullRequest("OPEN", None, pushed_at, red, resolved, head=head)


def pr_task(**extra):
    return {"id": "t1", "state": "pr", "pr_url": URL, "claimed_by": ME, **extra}


def test_resolved_checks_are_an_outcome_for_the_task_owner_once(store):
    found = pull()
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: found, now_ms=5) == [
        f"checks resolved green on {URL}, an outcome for {ME}"
    ]
    assert store.read(ME) == progress.Mark(outcome_at=5, outcome="checks resolved", talk=0)
    store.talk(ME)
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: found, now_ms=9) == []
    assert store.read(ME).talk == 1


def test_a_new_push_or_a_red_result_is_a_new_resolution(store):
    progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(), now_ms=5)
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(red=True), now_ms=6) == [
        f"checks resolved red on {URL}, an outcome for {ME}"
    ]
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: pull(red=True, pushed_at=200), now_ms=7)


@pytest.mark.parametrize(
    "task",
    [pr_task(state="claimed"), pr_task(pr_url=""), pr_task(claimed_by=""), {"id": "t1"}],
)
def test_only_held_pull_requests_with_an_owner_are_read(store, task):
    seen = []
    assert progress.checks_pass(store.redis, SLUG, [task], lambda url: seen.append(url) or pull()) == []
    assert seen == []


@pytest.mark.parametrize("found", [None, "pending"])
def test_unread_or_pending_checks_are_no_outcome(store, found):
    result = pull(resolved=False) if found else None
    assert progress.checks_pass(store.redis, SLUG, [pr_task()], lambda url: result) == []
    assert store.read(ME).outcome_at == 0


@pytest.mark.parametrize(
    ("rollup", "resolved", "red"),
    [
        ([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}], True, False),
        ([{"status": "COMPLETED", "conclusion": "FAILURE"}], True, True),
        ([{"status": "IN_PROGRESS", "conclusion": ""}], False, False),
        ([{"status": "QUEUED", "conclusion": None}], False, False),
        ([{"state": "PENDING"}], False, False),
        ([{"state": "EXPECTED"}], False, False),
        ([{"conclusion": "SUCCESS"}, {"state": "PENDING"}], False, False),
        ([], False, False),
    ],
)
def test_a_pull_request_reads_resolved_when_every_check_finished(rollup, resolved, red):
    from scripts.swarm.ledger_events import pull_request

    found = pull_request({"state": "OPEN", "statusCheckRollup": rollup})
    assert (found.resolved, found.red) == (resolved, red)


def test_no_command_is_no_outcome():
    assert progress.outcome_of(None) == ""


def test_an_outcome_without_a_time_is_stamped_now(store):
    import time

    before = int(time.time() * 1000)
    store.outcome(ME, "pushed")
    assert before <= store.read(ME).outcome_at <= int(time.time() * 1000)


def test_every_held_pull_request_is_read_past_a_skipped_one(store):
    other = "engineer@abcdef-0002"
    tasks = [
        pr_task(id="t0", state="claimed"),
        pr_task(id="t1", pr_url=URL + "1", claimed_by=other),
        pr_task(id="t2", pr_url=URL + "2"),
        pr_task(id="t3", pr_url=URL + "3", claimed_by="engineer@abcdef-0003"),
    ]
    found = {URL + "1": None, URL + "2": pull(), URL + "3": pull()}
    assert progress.checks_pass(store.redis, SLUG, tasks, found.get, now_ms=5) == [
        f"checks resolved green on {URL}2, an outcome for {ME}",
        f"checks resolved green on {URL}3, an outcome for engineer@abcdef-0003",
    ]
    assert store.read(other).outcome_at == 0


REPO, BRANCH = "https://github.com/o/r", "engineer-abcdef-0001"
LATER = 5 * 60_000
Read = Callable[[str, str], tuple[str, str] | None]


def branch_task(**extra: Any) -> dict[str, Any]:
    return {"id": "t2", "state": "claimed", "claimed_by": ME, "branch": BRANCH, "branch_repo": REPO, **extra}


def facts(*answers: tuple[str, str] | None) -> tuple[Read, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    queue = list(answers)

    def read(repo: str, branch: str) -> tuple[str, str] | None:
        calls.append((repo, branch))
        return queue.pop(0)

    return read, calls


def heads(
    store: Any,
    tasks: list[dict[str, Any]],
    now_ms: int,
    branch: Read,
    github: Callable[[str], Any] = lambda url: None,
    repo: str = "",
) -> list[str]:
    return progress.heads_pass(store.redis, SLUG, tasks, github, now_ms, repo, branch)


@pytest.mark.feature("tick-progress-heads")
def test_a_push_the_command_pattern_misses_is_still_an_outcome(store: Any) -> None:
    assert progress.outcome_of("cd /w && command git push origin HEAD") == ""
    read, calls = facts(("a", ""), ("b", ""))
    assert heads(store, [branch_task()], 100, read) == []
    assert store.read(ME) == progress.Mark(outcome_at=0, outcome="", talk=0)
    store.talk(ME)
    assert heads(store, [branch_task()], 100 + LATER, read) == [f"pushed b on {BRANCH}, an outcome for {ME}"]
    assert store.read(ME) == progress.Mark(outcome_at=100 + LATER, outcome="pushed", talk=0)
    assert calls == [(REPO, BRANCH), (REPO, BRANCH)]


@pytest.mark.feature("tick-progress-heads")
def test_a_first_push_of_a_new_branch_is_an_outcome(store: Any) -> None:
    read, _ = facts(("", ""), ("a", ""))
    assert heads(store, [branch_task()], 100, read) == []
    assert heads(store, [branch_task()], 100 + LATER, read) == [f"pushed a on {BRANCH}, an outcome for {ME}"]


@pytest.mark.feature("tick-progress-heads")
def test_a_deleted_branch_is_no_outcome(store: Any) -> None:
    read, _ = facts(("a", ""), ("", ""))
    heads(store, [branch_task()], 100, read)
    assert heads(store, [branch_task()], 100 + LATER, read) == []
    assert store.read(ME).outcome_at == 0


@pytest.mark.feature("tick-progress-heads")
def test_an_opened_pull_request_is_an_outcome(store: Any) -> None:
    read, _ = facts(("a", ""), ("a", URL))
    heads(store, [branch_task()], 100, read)
    assert heads(store, [branch_task()], 100 + LATER, read) == [f"pull request opened a on {URL}, an outcome for {ME}"]
    assert store.read(ME) == progress.Mark(outcome_at=100 + LATER, outcome="pull request opened", talk=0)


@pytest.mark.feature("tick-progress-heads")
def test_a_push_and_an_opened_pull_request_in_one_pass_end_on_the_push(store: Any) -> None:
    read, _ = facts(("a", ""), ("b", URL))
    heads(store, [branch_task()], 100, read)
    assert heads(store, [branch_task()], 100 + LATER, read) == [
        f"pull request opened b on {URL}, an outcome for {ME}",
        f"pushed b on {URL}, an outcome for {ME}",
    ]
    assert store.read(ME) == progress.Mark(outcome_at=100 + LATER, outcome="pushed", talk=0)


@pytest.mark.feature("tick-progress-heads")
def test_a_new_head_on_a_recorded_pull_request_is_a_push_once(store: Any) -> None:
    read, calls = facts()
    task = branch_task(pr_url=URL)
    assert heads(store, [task], 100, read, github=lambda url: pull(head="a")) == []
    assert heads(store, [task], 110, read, github=lambda url: pull(head="a")) == []
    assert heads(store, [task], 120, read, github=lambda url: pull(head="b")) == [
        f"pushed b on {URL}, an outcome for {ME}"
    ]
    assert heads(store, [task], 130, read, github=lambda url: pull(head="b")) == []
    assert store.read(ME).outcome_at == 120
    assert calls == []


@pytest.mark.feature("tick-progress-heads")
def test_a_recorded_pull_request_after_a_branch_read_is_an_opened_pull_request(store: Any) -> None:
    read, _ = facts(("a", ""))
    heads(store, [branch_task()], 100, read)
    assert heads(store, [branch_task(pr_url=URL)], 110, read, github=lambda url: pull(head="a")) == [
        f"pull request opened a on {URL}, an outcome for {ME}"
    ]


@pytest.mark.feature("tick-progress-heads")
@pytest.mark.parametrize("found", [None, pull(head="")])
def test_an_unread_pull_request_records_nothing(store: Any, found: Any) -> None:
    read, _ = facts()
    assert heads(store, [branch_task(pr_url=URL)], 100, read, github=lambda url: found) == []
    assert store.redis.hget(store.key(ME), f"{progress.HEAD}:t2") is None


@pytest.mark.feature("tick-progress-heads")
def test_a_failed_branch_read_keeps_no_baseline(store: Any) -> None:
    read, _ = facts(None, ("a", ""), ("b", ""))
    assert heads(store, [branch_task()], 100, read) == []
    assert store.redis.hget(store.key(ME), f"{progress.HEAD}:t2") is None
    assert heads(store, [branch_task()], 100 + LATER, read) == []
    assert heads(store, [branch_task()], 100 + 2 * LATER, read) == [f"pushed b on {BRANCH}, an outcome for {ME}"]


@pytest.mark.feature("tick-progress-heads")
def test_a_branch_is_read_at_most_once_per_lookup_window(store: Any) -> None:
    read, calls = facts(("a", ""), ("a", ""))
    heads(store, [branch_task()], 100, read)
    heads(store, [branch_task()], 99 + LATER, read)
    assert len(calls) == 1
    heads(store, [branch_task()], 100 + LATER, read)
    assert len(calls) == 2


@pytest.mark.feature("tick-progress-heads")
def test_each_task_keeps_its_own_lookup_window(store: Any) -> None:
    read, calls = facts(("a", ""), ("c", ""))
    tasks = [branch_task(id="t1", branch="one"), branch_task(id="t3", branch="two")]
    heads(store, tasks, 100, read)
    assert calls == [(REPO, "one"), (REPO, "two")]


@pytest.mark.feature("tick-progress-heads")
def test_each_task_keeps_its_own_head(store: Any) -> None:
    read, _ = facts(("a", ""), ("c", ""), ("b", ""), ("c", ""))
    tasks = [branch_task(id="t1", branch="one"), branch_task(id="t3", branch="two")]
    assert heads(store, tasks, 100, read) == []
    assert heads(store, tasks, 100 + LATER, read) == [f"pushed b on one, an outcome for {ME}"]


@pytest.mark.feature("tick-progress-heads")
def test_the_claimant_worktree_branch_on_the_swarm_repository_is_read_when_none_is_recorded(store: Any) -> None:
    read, calls = facts(("a", ""), ("b", ""))
    task = {"id": "t2", "state": "pr", "claimed_by": ME}
    heads(store, [task], 100, read, repo="https://github.com/o/swarm")
    assert heads(store, [task], 100 + LATER, read, repo="https://github.com/o/swarm") == [
        f"pushed b on its worktree branch, an outcome for {ME}"
    ]
    assert calls == [("https://github.com/o/swarm", BRANCH)] * 2


@pytest.mark.feature("tick-progress-heads")
@pytest.mark.parametrize(
    "task",
    [
        branch_task(state="done"),
        branch_task(state="open"),
        branch_task(claimed_by=""),
        branch_task(branch_repo=""),
        {"id": "t2"},
    ],
)
def test_only_held_tasks_with_a_claimant_and_a_repository_are_read(store: Any, task: dict[str, Any]) -> None:
    read, calls = facts(("a", ""))
    assert heads(store, [task], 100, read) == []
    assert calls == []


@pytest.mark.feature("tick-progress-heads")
def test_a_new_claimant_starts_from_a_baseline(store: Any) -> None:
    read, _ = facts(("a", ""), ("b", ""))
    heads(store, [branch_task(id="t1")], 100, read)
    assert heads(store, [branch_task()], 100 + LATER, read) == []
    assert store.read(ME).outcome_at == 0


@pytest.mark.feature("tick-progress-heads")
def test_branch_head_reads_the_branch_commit_and_its_open_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import merge_queue

    seen: list[dict[str, str]] = []
    data = {"repository": {"ref": {"target": {"oid": "abc"}}, "pullRequests": {"nodes": [{"url": URL}]}}}
    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables: seen.append(variables) or data)
    assert progress.branch_head(REPO + ".git", BRANCH) == ("abc", URL)
    assert seen == [{"owner": "o", "name": "r", "branch": BRANCH, "ref": f"refs/heads/{BRANCH}"}]


@pytest.mark.feature("tick-progress-heads")
@pytest.mark.parametrize(
    ("data", "found"),
    [
        ({"repository": {"ref": None, "pullRequests": {"nodes": []}}}, ("", "")),
        ({"repository": None}, ("", "")),
    ],
)
def test_branch_head_reads_a_missing_branch_as_empty(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any], found: tuple[str, str]
) -> None:
    from scripts.swarm import merge_queue

    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables: data)
    assert progress.branch_head(REPO, BRANCH) == found


@pytest.mark.feature("tick-progress-heads")
def test_branch_head_is_unread_when_github_fails_or_the_repository_is_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import merge_queue
    from scripts.swarm.store import SwarmError

    def fail(query: str, variables: dict[str, str]) -> dict[str, Any]:
        raise SwarmError("GitHub API failed")

    monkeypatch.setattr(merge_queue, "graphql", fail)
    assert progress.branch_head(REPO, BRANCH) is None
    assert progress.branch_head("https://gitlab.com/o/r", BRANCH) is None
    assert progress.branch_head(REPO, "") is None


@pytest.mark.feature("tick-progress-heads")
def test_origin_reads_the_public_origin_address_of_a_checkout() -> None:
    from subprocess import CompletedProcess

    seen: list[list[str]] = []

    def run(argv: list[str], **options: Any) -> CompletedProcess[str]:
        seen.append(argv)
        return CompletedProcess(argv, 0, "https://hive@github.com/o/r.git\n", "")

    assert progress.origin("/repo", run) == "https://github.com/o/r.git"
    assert seen == [["git", "-C", "/repo", "remote", "get-url", "origin"]]
    assert progress.origin("/repo", lambda argv, **options: CompletedProcess(argv, 2, "", "no remote")) == ""
    assert progress.origin("", run) == ""
    assert len(seen) == 1


@pytest.mark.feature("tick-progress-origin-without-git")
def test_origin_is_empty_where_git_is_not_installed() -> None:
    def run(argv: list[str], **options: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert progress.origin("/repo", run) == ""
