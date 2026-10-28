import json
import subprocess

import pytest

from scripts.swarm import cli, ledger_events, merge_queue, queue_exit, waits
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/7"
OPEN = {
    "id": "PR_one",
    "state": "OPEN",
    "headRefOid": "abc",
    "baseRefName": "dev",
    "mergeQueueEntry": None,
    "isDraft": False,
    "reviewDecision": None,
    "statusCheckRollup": {"state": "SUCCESS"},
    "repository": {"mergeQueue": {"id": "MQ_dev"}},
}
ENTRY = {"id": "MQ_one", "position": 2, "state": "AWAITING_CHECKS"}
AUTHORED = {"sha": "abc", "commit": {"message": "Change the code"}, "committer": {"login": "nestorcolt"}}
REFRESHED = {"sha": "abc", "commit": {"message": "Merge branch 'dev' into ci-1"}, "committer": {"login": "web-flow"}}


READ_PULL = ledger_events.view


def runner(*responses):
    calls = []
    pending = iter(responses)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:2] == ["gh", "api"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20}
        if command[2:] == ["graphql", "-f", f"query={merge_queue.FRESHNESS}", "-f", f"url={URL}"]:
            response = {
                "data": {
                    "resource": {
                        "baseRefName": "dev",
                        "baseRef": {"branchProtectionRule": None},
                        "repository": {"nameWithOwner": "o/r"},
                    }
                },
            }
        elif command[2] == "repos/o/r/rules/branches/dev":
            response = [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "CI Validate"}],
                    },
                }
            ]
        else:
            response = next(pending)
            assert command[2] == "graphql"
            assert command[4] in [
                f"query={query}"
                for query in (
                    merge_queue.STATE,
                    merge_queue.QUEUE,
                    merge_queue.DEQUEUE,
                    ledger_events.PULL_QUERY.replace("$number", "7"),
                )
            ]
        return subprocess.CompletedProcess(command, 0, response if isinstance(response, str) else json.dumps(response))

    return run, calls


def checked_resource(conclusion="SUCCESS", head="abc"):
    return {
        "data": {
            "resource": {
                "state": "OPEN",
                "headRefOid": head,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "id": "commit",
                                "committedDate": "2026-10-15T15:00:00Z",
                                "statusCheckRollup": {
                                    "contexts": {
                                        "nodes": [
                                            {"name": "CI Validate", "conclusion": conclusion, "isRequired": True},
                                            {"name": "Optional report", "conclusion": "FAILURE", "isRequired": False},
                                        ],
                                    },
                                },
                                "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                            },
                        }
                    ],
                },
            },
        },
    }


@pytest.mark.parametrize(
    "repo, number, workflow",
    [("the-cloud-clockwork/antoncore", "42", "CI Validate"), ("the-cloud-clockwork/agentibrain-kernel", "127", "ci")],
)
def test_queue_adds_another_repositorys_pull_request_to_its_own_merge_queue(repo, number, workflow):
    url = f"https://github.com/{repo}/pull/{number}"
    rest_paths, enqueued = [], []

    def run(command, **kwargs):
        if command[2] != "graphql":
            rest_paths.append(command[2])
            if command[2] != f"repos/{repo}/rules/branches/dev":
                return subprocess.CompletedProcess(command, 1, "", "gh: Not Found (HTTP 404)")
            response = [
                {"type": "required_linear_history"},
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "Gate — Required", "integration_id": 15368}]},
                },
                {"type": "merge_queue", "parameters": {"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN"}},
            ]
        elif command[4] == f"query={merge_queue.STATE}":
            assert command[5:] == ["-f", f"url={url}"]
            response = {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY if enqueued else None}}}
        elif command[4] == f"query={merge_queue.FRESHNESS}":
            response = {
                "data": {
                    "resource": {
                        "repository": {"nameWithOwner": repo},
                        "baseRefName": "dev",
                        "baseRef": {"branchProtectionRule": None},
                    }
                }
            }
        elif command[4] == f"query={merge_queue.QUEUE}":
            enqueued.append(command[5:])
            response = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": ENTRY["id"]}}}}
        else:
            assert command[4] == f"query={ledger_events.PULL_QUERY.replace('$number', number)}"
            assert command[5:] == ["-f", f"url={url}"]
            response = checked_resource()
            check = response["data"]["resource"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"][
                "nodes"
            ][0]
            check["name"] = "Gate — Required"
            check["checkSuite"] = {"app": {"databaseId": 15368}, "workflowRun": {"workflow": {"name": workflow}}}
        return subprocess.CompletedProcess(command, 0, json.dumps(response))

    result = merge_queue.operate("queue", url, run)

    assert enqueued == [["-f", "id=PR_one", "-f", "head=abc"]]
    assert result == {"url": url, "state": "OPEN", "head": "abc", "queued": True, "entry": ENTRY}
    assert rest_paths and all(path.startswith(f"repos/{repo}/") for path in rest_paths)


@pytest.mark.parametrize(
    "conclusion, optional_running, expected",
    [("SUCCESS", True, True), (None, False, False), ("FAILURE", False, False), ("NEUTRAL", False, True)],
)
def test_queue_waits_for_all_and_only_required_checks(conclusion, optional_running, expected):
    response = checked_resource(conclusion)
    commit = response["data"]["resource"]["commits"]["nodes"][0]["commit"]
    commit["file"] = {"object": {"entries": [{"object": {"text": "jobs:\n  gate:\n    name: Gate — Required\n"}}]}}
    commit["statusCheckRollup"]["contexts"]["nodes"].append(
        {"name": "Gate — Required", "conclusion": "SUCCESS", "isRequired": True}
    )
    if optional_running:
        commit["checkSuites"]["nodes"] = [{"status": "IN_PROGRESS", "workflowRun": {"databaseId": 1}}]
    run, calls = runner(
        {"data": {"resource": OPEN}},
        response,
        {"data": {}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )
    if expected:
        assert merge_queue.operate("queue", URL, run)["queued"]
    else:
        with pytest.raises(SwarmError, match="required checks"):
            merge_queue.operate("queue", URL, run)
        assert not any("enqueuePullRequest(input:" in str(command) for command, _ in calls)


@pytest.mark.parametrize("missing", ["all", "one"])
def test_queue_waits_until_every_configured_check_exists(missing):
    response = checked_resource()
    commit = response["data"]["resource"]["commits"]["nodes"][0]["commit"]
    commit["statusCheckRollup"]["contexts"]["nodes"] = (
        [] if missing == "all" else [{"name": "Other required", "conclusion": "SUCCESS", "isRequired": True}]
    )
    run, calls = runner({"data": {"resource": OPEN}}, response)
    with pytest.raises(SwarmError, match="required checks"):
        merge_queue.operate("queue", URL, run)
    assert not any("enqueuePullRequest(input:" in str(command) for command, _ in calls)


@pytest.mark.parametrize(
    "source, state, expected",
    [
        ("status", "SUCCESS", True),
        ("status", "PENDING", False),
        ("status", "FAILURE", False),
        ("status", "ERROR", False),
        ("other app", "SUCCESS", False),
        ("required app", "SUCCESS", True),
        ("both apps", "SUCCESS", True),
    ],
)
@pytest.mark.parametrize("policy", ["rules", "classic"])
def test_queue_checks_the_app_required_by_repository_policy(source, state, expected, policy):
    response = checked_resource()
    commit = response["data"]["resource"]["commits"]["nodes"][0]["commit"]
    if source == "status":
        check = {"context": "CI Validate", "state": state, "isRequired": True}
    else:
        check = {
            "name": "CI Validate",
            "conclusion": "SUCCESS",
            "isRequired": True,
            "checkSuite": {"app": {"databaseId": 15368 if source != "other app" else 42}},
        }
    commit["statusCheckRollup"]["contexts"]["nodes"] = [check]
    if source == "both apps":
        commit["statusCheckRollup"]["contexts"]["nodes"].append(
            {
                "name": "CI Validate",
                "databaseId": 2,
                "conclusion": "FAILURE",
                "isRequired": True,
                "checkSuite": {"app": {"databaseId": 42}},
            }
        )
    reader, _ = runner(response)
    assert READ_PULL(URL, reader, required_checks={"CI Validate": 15368}) == ledger_events.PullRequest(
        "OPEN",
        None,
        None,
        state in {"FAILURE", "ERROR"},
        resolved=expected,
        failed=("CI Validate",) if state in {"FAILURE", "ERROR"} else (),
        head="abc",
    )
    original, calls = runner(
        {"data": {"resource": OPEN}},
        response,
        {"data": {}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )

    def run(command, **kwargs):
        if command[2] == "repos/o/r/rules/branches/dev":
            rules = (
                []
                if policy == "classic"
                else [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": "CI Validate", "integration_id": 15368}],
                        },
                    }
                ]
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(rules))
        if policy == "classic" and command[2] == "graphql" and command[4] == f"query={merge_queue.FRESHNESS}":
            protection = {
                "requiredStatusCheckContexts": ["CI Validate"],
                "requiredStatusChecks": [
                    {"context": "CI Validate", "app": {"databaseId": 15368}},
                ],
            }
            metadata = {
                "baseRefName": "dev",
                "repository": {"nameWithOwner": "o/r"},
                "baseRef": {"branchProtectionRule": protection},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps({"data": {"resource": metadata}}))
        return original(command, **kwargs)

    if expected:
        assert merge_queue.operate("queue", URL, run)["queued"]
    else:
        with pytest.raises(SwarmError, match="required checks"):
            merge_queue.operate("queue", URL, run)
        assert not any("enqueuePullRequest(input:" in str(command) for command, _ in calls)


@pytest.mark.parametrize("present", [True, False])
def test_queue_reads_classic_branch_requirements(present):
    response = checked_resource()
    check = response["data"]["resource"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
    check["name"] = "Legacy required" if present else "Different required"
    original, calls = runner(
        {"data": {"resource": OPEN}},
        response,
        {"data": {}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )

    def run(command, **kwargs):
        if command[2] == "repos/o/r/rules/branches/dev":
            return subprocess.CompletedProcess(command, 0, "[]")
        if command[2] == "graphql" and command[4] == f"query={merge_queue.FRESHNESS}":
            metadata = {
                "baseRefName": "dev",
                "repository": {"nameWithOwner": "o/r"},
                "baseRef": {"branchProtectionRule": {"requiredStatusCheckContexts": ["Legacy required"]}},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps({"data": {"resource": metadata}}))
        return original(command, **kwargs)

    if present:
        assert merge_queue.operate("queue", URL, run)["queued"]
    else:
        with pytest.raises(SwarmError, match="required checks"):
            merge_queue.operate("queue", URL, run)
        assert not any("enqueuePullRequest(input:" in str(command) for command, _ in calls)


def test_queue_allows_a_repository_with_no_required_checks():
    response = checked_resource()
    checks = response["data"]["resource"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
    for check in checks:
        check["isRequired"] = False
    original, _ = runner(
        {"data": {"resource": OPEN}},
        response,
        {"data": {}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )

    def run(command, **kwargs):
        if command[2] == "repos/o/r/rules/branches/dev":
            return subprocess.CompletedProcess(command, 0, "[]")
        return original(command, **kwargs)

    assert merge_queue.operate("queue", URL, run)["queued"]


@pytest.mark.parametrize(
    "checks, expected, red, resolved, failed",
    [
        (
            [
                {"name": "Build", "conclusion": "FAILURE", "isRequired": True},
                {"name": "Lint", "conclusion": "FAILURE", "isRequired": True},
            ],
            {"Build", "Lint"},
            True,
            False,
            ("Build", "Lint"),
        ),
        ([{"context": "Legacy check", "state": "SUCCESS", "isRequired": True}], {"Legacy check"}, False, True, ()),
        (
            [{"context": "Legacy check", "state": "FAILURE", "isRequired": True}],
            {"Legacy check"},
            True,
            False,
            ("Legacy check",),
        ),
        ([{"conclusion": "FAILURE", "isRequired": True}], {"Expected check"}, True, False, ("a check",)),
    ],
)
def test_required_check_view_preserves_results_and_check_names(checks, expected, red, resolved, failed):
    response = checked_resource()
    commit = response["data"]["resource"]["commits"]["nodes"][0]["commit"]
    commit["statusCheckRollup"]["contexts"]["nodes"] = checks
    run, _ = runner(response)
    assert READ_PULL(URL, run, required_checks=dict.fromkeys(expected)) == ledger_events.PullRequest(
        "OPEN", None, None, red, resolved=resolved, failed=failed, head="abc"
    )


def test_queue_passes_the_observed_head_to_intent_admission():
    run, _ = runner(
        {"data": {"resource": OPEN}},
        checked_resource(),
        {"data": {}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )
    seen = []
    assert merge_queue.operate("queue", URL, run, admit=seen.append)["queued"]
    assert seen == ["abc"]


@pytest.mark.parametrize(
    "failure, expected",
    [
        (OSError("missing gh"), "GitHub API: missing gh"),
        ((1, "denied\n"), "denied"),
        ((1, ""), "GitHub API failed"),
    ],
)
def test_queue_reports_failure_reading_required_check_results(failure, expected):
    original, _ = runner({"data": {"resource": OPEN}})

    def run(command, **kwargs):
        if command[2] == "graphql" and command[4] == f"query={ledger_events.PULL_QUERY.replace('$number', '7')}":
            if isinstance(failure, Exception):
                raise failure
            code, stderr = failure
            return subprocess.CompletedProcess(command, code, "", stderr)
        return original(command, **kwargs)

    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, run)
    assert str(caught.value) == expected


def test_queue_reports_an_unreadable_head_without_inventing_a_failed_check():
    run, _ = runner({"data": {"resource": OPEN}}, {"data": {"resource": None}})
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, run)
    assert str(caught.value) == "required checks on the current pull request head must pass before queueing"


def test_queue_names_all_failing_required_checks():
    response = checked_resource("FAILURE")
    commit = response["data"]["resource"]["commits"]["nodes"][0]["commit"]
    commit["statusCheckRollup"]["contexts"]["nodes"].append(
        {"name": "Lint", "conclusion": "FAILURE", "isRequired": True}
    )
    original, _ = runner({"data": {"resource": OPEN}}, response)

    def run(command, **kwargs):
        if command[2] == "repos/o/r/rules/branches/dev":
            rules = [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "CI Validate"}, {"context": "Lint"}],
                    },
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(rules))
        return original(command, **kwargs)

    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, run)
    assert str(caught.value) == (
        "required checks on the current pull request head must pass before queueing; failed: CI Validate, Lint"
    )


@pytest.fixture(autouse=True)
def task_ledger(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(ledger_events, "view", READ_PULL)

    def tasks(slug):
        assert slug == "sw"
        return [{"id": "t1", "pr_url": URL}]

    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=tasks))


def test_state_reports_an_open_pull_request_without_a_queue_entry():
    run, calls = runner({"data": {"resource": OPEN}})
    assert merge_queue.operate("state", URL, run) == {
        "url": URL,
        "state": "OPEN",
        "head": "abc",
        "queued": False,
        "entry": None,
    }
    command, kwargs = calls[0]
    assert command[:3] == ["gh", "api", "graphql"]
    assert command[-2:] == ["-f", f"url={URL}"]
    assert "resource(url: $url)" in command[4]
    assert "id state headRefOid baseRefName mergeQueueEntry { id position state }" in command[4]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 20}


def test_queue_enqueues_the_observed_head_and_reports_the_queue_entry():
    run, calls = runner(
        {"data": {"resource": OPEN}},
        checked_resource(),
        {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )
    assert merge_queue.operate("queue", URL, run) == {
        "url": URL,
        "state": "OPEN",
        "head": "abc",
        "queued": True,
        "entry": ENTRY,
    }
    command, _ = calls[4]
    assert command[:3] == ["gh", "api", "graphql"]
    assert "enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head})" in command[4]
    assert command[5:] == ["-f", "id=PR_one", "-f", "head=abc"]
    assert calls[-1][0] == calls[0][0]


@pytest.mark.parametrize("review", [None, "APPROVED"])
def test_queue_squash_merges_the_observed_head_when_the_repository_has_no_queue(review):
    calls = []
    responses = iter(
        [
            {"data": {"resource": {**OPEN, "repository": {"mergeQueue": None}, "reviewDecision": review}}},
            checked_resource(),
            {"data": {"mergePullRequest": {"pullRequest": {"state": "MERGED"}}}},
            {"data": {"resource": {**OPEN, "state": "MERGED", "repository": {"mergeQueue": None}}}},
        ]
    )

    policy, _ = runner()

    def run(command, **kwargs):
        calls.append(command)
        if command[2] != "graphql" or command[4] == f"query={merge_queue.FRESHNESS}":
            return policy(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, json.dumps(next(responses)))

    assert merge_queue.operate("queue", URL, run) == {
        "url": URL,
        "state": "MERGED",
        "head": "abc",
        "queued": False,
        "entry": None,
        "merge_commit": None,
    }
    assert len(calls) == 6
    assert "mergePullRequest(input:" in calls[4][4]
    assert "expectedHeadOid: $head" in calls[4][4]
    assert "mergeMethod: SQUASH" in calls[4][4]
    assert calls[4][5:] == ["-f", "id=PR_one", "-f", "head=abc"]


@pytest.mark.parametrize(
    "change",
    [
        {"isDraft": True},
        {"reviewDecision": "CHANGES_REQUESTED"},
        {"reviewDecision": "REVIEW_REQUIRED"},
    ],
)
def test_queue_without_a_queue_refuses_an_unready_pull_request(change):
    run, calls = runner({"data": {"resource": {**OPEN, "repository": {"mergeQueue": None}, **change}}})
    with pytest.raises(
        SwarmError, match="^squash merge requires a ready pull request with green checks and no pending review$"
    ):
        merge_queue.operate("queue", URL, run)
    assert len(calls) == 1


@pytest.mark.parametrize("conclusion", [None, "FAILURE", "CANCELLED"])
def test_queue_waits_for_green_checks_on_the_current_head(conclusion):
    run, calls = runner({"data": {"resource": OPEN}}, checked_resource(conclusion))
    with pytest.raises(SwarmError, match="^required checks on the current pull request head must pass before queueing"):
        merge_queue.operate("queue", URL, run)
    assert not any("update-branch" in str(call[0]) or "enqueuePullRequest(input:" in str(call[0]) for call in calls)


@pytest.mark.parametrize("head", [None, "older"])
def test_queue_requires_checks_on_its_observed_head(head):
    run, _ = runner({"data": {"resource": OPEN}}, checked_resource(head=head))
    with pytest.raises(SwarmError, match="^required checks on the current pull request head must pass before queueing"):
        merge_queue.operate("queue", URL, run)


def dev_moved(head=AUTHORED, changed=(), conclusion="success", jobs=None, log="", refuse=""):
    calls = []
    test_count = [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20}
        path, queued = command[2], any(f"query={merge_queue.QUEUE}" in call for call in calls)
        if path == "graphql" and command[4] == f"query={merge_queue.QUEUE}":
            if refuse:
                return subprocess.CompletedProcess(command, 1, "", refuse)
            reply = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}}
        elif path == "graphql" and command[4] == f"query={merge_queue.STATE}":
            reply = {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY if queued else None}}}
        elif path == "graphql" and command[4] == f"query={merge_queue.FRESHNESS}":
            reply = {
                "data": {
                    "resource": {
                        "baseRefName": "dev",
                        "baseRef": {"branchProtectionRule": None},
                        "repository": {"nameWithOwner": "o/r"},
                    }
                }
            }
        elif path == "repos/o/r/rules/branches/dev":
            reply = [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "CI Validate"}],
                    },
                }
            ]
        elif path == "graphql":
            reply = checked_resource(conclusion.upper() if conclusion else None)

        elif path.endswith("/logs"):
            return subprocess.CompletedProcess(command, 0, log)
        elif path.endswith("/jobs?per_page=100"):
            reply = {"jobs": test_count if jobs is None else jobs}
        elif path == "repos/o/r/commits/abc":
            reply = head
        elif "/compare/" in path:
            reply = {"files": list(changed), "total_commits": 1}
        else:
            reply = {"message": "Updating pull request branch."}
        return subprocess.CompletedProcess(command, 0, json.dumps(reply))

    return run, calls


def assert_left_dev_alone(calls):
    paths = [call[2] for call in calls]
    assert not any(
        part in path for path in paths for part in ("/jobs", "/logs", "/commits/", "/compare/", "update-branch")
    )


def assert_enqueued_as_is(result, calls):
    assert result == {"url": URL, "state": "OPEN", "head": "abc", "queued": True, "entry": ENTRY}
    enqueued = [call for call in calls if call[2] == "graphql" and call[4] == f"query={merge_queue.QUEUE}"]
    assert [call[5:] for call in enqueued] == [["-f", "id=PR_one", "-f", "head=abc"]]
    assert_left_dev_alone(calls)


@pytest.mark.parametrize(
    "changed",
    [
        [{"filename": ".github/workflows/test.yml"}],
        [{"filename": "scripts/ci_mutation/__init__.py"}],
        [{"filename": "scripts/check_gate.py"}],
        [{"filename": "tests/swarm/test_tick.py"}],
        [{"filename": "docs/old.py", "previous_filename": "tests/old.py"}],
        [{"filename": f"hooks/item{i}.py"} for i in range(300)],
        [{"filename": "scripts/swarm/intent.py"}],
    ],
)
@pytest.mark.parametrize(
    "head",
    [
        AUTHORED,
        {**AUTHORED, "commit": {"message": "Merge branch 'dev' into ci-1"}},
        REFRESHED,
        {**REFRESHED, "commit": {"message": "Merge pull request #3 from o/side"}},
        {**REFRESHED, "committer": None},
        {**REFRESHED, "commit": {"message": "Merge branch 'dev' from o/side"}},
    ],
)
def test_queue_enqueues_a_green_head_as_is_whatever_dev_changed(changed, head):
    run, calls = dev_moved(head, changed)
    assert_enqueued_as_is(merge_queue.operate("queue", URL, run), calls)


@pytest.mark.parametrize("conclusion", [None, "failure"])
def test_queue_blocks_a_red_head_without_reading_dev(conclusion):
    run, calls = dev_moved(REFRESHED, [{"filename": "tests/swarm/test_tick.py"}], conclusion)
    with pytest.raises(SwarmError, match="^required checks on the current pull request head must pass before queueing"):
        merge_queue.operate("queue", URL, run)
    assert not any(call[2] == "graphql" and call[4] == f"query={merge_queue.QUEUE}" for call in calls)
    assert_left_dev_alone(calls)


@pytest.mark.parametrize("log", ["", "2026-10-09T09:20:06Z   BASE: origin/dev\n"])
def test_queue_enqueues_without_the_base_its_checks_graded(log):
    run, calls = dev_moved(log=log)
    assert_enqueued_as_is(merge_queue.operate("queue", URL, run), calls)


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"id": 31, "name": "wrong job", "conclusion": "success"}],
        [{"id": 31, "name": "test-count (3.11)", "conclusion": "failure"}],
    ],
)
def test_queue_enqueues_without_a_green_test_count_job(jobs):
    run, calls = dev_moved(jobs=jobs)
    assert_enqueued_as_is(merge_queue.operate("queue", URL, run), calls)


def test_a_refused_enqueue_names_github_and_leaves_the_branch_alone():
    run, calls = dev_moved(refuse="GraphQL: Pull request is not mergeable\n")
    with pytest.raises(SwarmError) as caught:
        merge_queue.operate("queue", URL, run)
    assert str(caught.value) == "GraphQL: Pull request is not mergeable"
    assert_left_dev_alone(calls)


@pytest.mark.parametrize("stderr, message", [("denied\n", "denied"), ("", "GitHub API failed")])
def test_queue_reports_rest_api_failure(stderr, message):
    initial, calls = runner(
        {"data": {"resource": OPEN}},
    )

    def run(command, **kwargs):
        if len(calls) < 1:
            return initial(command, **kwargs)
        return subprocess.CompletedProcess(command, 1, "", stderr)

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("queue", URL, run)
    assert str(exc.value) == message


@pytest.mark.parametrize("error", [OSError("missing gh"), subprocess.TimeoutExpired("gh", 20)])
def test_queue_reports_rest_transport_failure(error):
    initial, calls = runner(
        {"data": {"resource": OPEN}},
    )

    def run(command, **kwargs):
        if len(calls) < 1:
            return initial(command, **kwargs)
        raise error

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("queue", URL, run)
    assert str(exc.value) == f"GitHub API: {error}"


def test_a_job_log_read_allows_escape_sequences_and_returns_text():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        log = command[2].endswith("/logs")
        return subprocess.CompletedProcess(command, 0, "\x1b[31mFAILED\x1b[0m\n" if log else "{}", "")

    assert merge_queue.rest("repos/o/r/actions/jobs/5/logs", run) == "\x1b[31mFAILED\x1b[0m\n"
    assert merge_queue.rest("repos/o/r/actions/runs/5", run) == {}
    assert calls == [
        ["gh", "api", "repos/o/r/actions/jobs/5/logs", "--allow-escape-sequences"],
        ["gh", "api", "repos/o/r/actions/runs/5"],
    ]


def test_dequeue_removes_the_pull_request_then_reports_its_state():
    run, calls = runner(
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
        {"data": {"dequeuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}},
        {"data": {"resource": OPEN}},
    )
    assert merge_queue.operate("dequeue", URL, run) == {
        "url": URL,
        "state": "OPEN",
        "head": "abc",
        "queued": False,
        "entry": None,
    }
    command, _ = calls[1]
    assert "dequeuePullRequest(input: {id: $id})" in command[4]
    assert command[5:] == ["-f", "id=PR_one"]
    assert (
        calls[2][0] == calls[0][0] == ["gh", "api", "graphql", "-f", f"query={merge_queue.STATE}", "-f", f"url={URL}"]
    )


def swarm_of(lane):
    from types import SimpleNamespace

    import fakeredis

    from scripts.swarm.store import RedisStore

    metadata = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = SimpleNamespace(name="engineer@a1b2c3-0001", lane=lane, task="t1")
    names = SimpleNamespace(swarm_slug=lambda slug: slug, resolve=lambda name: name)
    return SimpleNamespace(names=names, agents=lambda slug: [agent], redis=metadata.redis, key=metadata.key)


@pytest.mark.parametrize("action", ["queue", "dequeue", "state"])
def test_cli_routes_each_operation_and_prints_its_state(monkeypatch, capsys, action):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    seen = []

    def operate(verb, url, **kwargs):
        seen.append((verb, url, kwargs["journal"].url, kwargs.get("attempts")))
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", action, URL]) == 0
    assert seen == [(action, URL, URL, 40 if action == "queue" else None)]
    assert capsys.readouterr().out == '{"queued": true}\n'


@pytest.mark.parametrize("failed", [False, True])
def test_cli_queue_refuses_a_head_whose_last_group_failed_after_intent(monkeypatch, capsys, failed):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    seen = []

    def fresh(url, head):
        seen.append(("fresh", url, head))
        if failed:
            raise SwarmError(f"head {head} failed its last merge group run (Tests run)")

    monkeypatch.setattr(queue_exit, "require_fresh", fresh)
    monkeypatch.setattr(cli, "_queue_intent", lambda store, args, agent, head: seen.append(("intent", head)))
    queued = []
    monkeypatch.setattr(
        merge_queue, "operate", lambda verb, url, **kwargs: kwargs["admit"]("abc") or queued.append(url) or {}
    )

    assert cli.main(["sw", "merge", "queue", URL]) == (1 if failed else 0)
    assert seen == [("intent", "abc"), ("fresh", URL, "abc")]
    assert queued == ([] if failed else [URL])
    if failed:
        assert "head abc failed its last merge group run (Tests run)" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_cli_accepts_the_earlier_pull_the_task_lists(monkeypatch, action):
    from types import SimpleNamespace

    first = "https://github.com/o/r/pull/6"
    task = {"id": "t1", "pr_url": URL, "split": ["1-2", "3-4"], "split_pulls": [first, URL]}
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: [task]))
    seen = []
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url, **kwargs: seen.append((verb, url)) or {})
    assert cli.main(["sw", "merge", action, first]) == 0
    assert seen == [(action, first)]


def test_cli_refuses_an_unlisted_pull_before_a_queue_call(monkeypatch):
    from types import SimpleNamespace

    task = {"id": "t1", "pr_url": URL, "split": ["1-2", "3-4"], "split_pulls": ["https://github.com/o/r/pull/6", URL]}
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: [task]))
    seen = []
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url, **kwargs: seen.append((verb, url)) or {})
    assert cli.main(["sw", "merge", "queue", "https://github.com/o/r/pull/99"]) == 1
    assert seen == []


@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_cli_refuses_distributed_final_mutations_before_provider_calls(monkeypatch, action):
    state = swarm_of("eng")
    monkeypatch.setattr(cli, "connect", lambda: state)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "operate", lambda *args: pytest.fail("provider mutation"))
    import fakeredis

    from scripts.swarm.store import RedisStore

    metadata = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    state.redis, state.key = metadata.redis, metadata.key
    state.redis.set(state.key("sw", "task-authority", "t1"), "{}")
    assert cli.main(["sw", "merge", action, URL]) == 1


@pytest.mark.parametrize("same_link", [False, True])
def test_cli_refuses_another_distributed_tasks_pull_request(monkeypatch, same_link):
    from types import SimpleNamespace

    state = swarm_of("eng")
    rows = [
        {"id": "t1", "pr_url": URL if same_link else "https://github.com/o/r/pull/8"},
        {"id": "t2", "pr_url": URL},
    ]
    monkeypatch.setattr(cli, "connect", lambda: state)
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: rows))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "operate", lambda *args: pytest.fail("foreign provider mutation"))
    state.redis.set(state.key("sw", "task-authority", "t2"), "{}")
    assert cli.main(["sw", "merge", "queue", URL]) == 1


@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_cli_refuses_the_master_a_queue_change(monkeypatch, capsys, action):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("master"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url, **kwargs: pytest.fail("the master changed the queue"))

    assert cli.main(["sw", "merge", action, URL]) == 1
    assert capsys.readouterr().err == "swarm: engineer@a1b2c3-0001 is the swarm master; the master works no task\n"


def test_cli_lets_the_master_read_queue_state(monkeypatch, capsys):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("master"))
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url, **kwargs: {"queued": False})

    assert cli.main(["sw", "merge", "state", URL]) == 0
    assert capsys.readouterr().out == '{"queued": false}\n'


def test_cli_queue_declares_no_checks_wait(monkeypatch, capsys):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(
        merge_queue, "operate", lambda action, url, **kwargs: {"url": url, "queued": True, "entry": ENTRY}
    )
    seen = []
    monkeypatch.setattr(cli.idle, "declare_wait", lambda *args, **kwargs: seen.append((args, kwargs)))
    assert cli.main(["sw", "merge", "queue", URL]) == 0
    assert seen == []
    assert json.loads(capsys.readouterr().out) == {"url": URL, "queued": True, "entry": ENTRY}


@pytest.mark.parametrize(
    "mode, code, warned, logged",
    [
        ("enforce", 1, True, "deny"),
        ("coach", 0, True, "coach"),
        ("observe", 0, False, "observe"),
        ("off", 0, False, None),
    ],
)
def test_cli_queue_applies_the_judged_intent_mode(monkeypatch, tmp_path, capsys, mode, code, warned, logged):
    from types import SimpleNamespace

    from scripts.gates import intent
    from scripts.gates.verdicts import Verdicts

    store = swarm_of("eng")

    def config(slug):
        assert slug == "sw"
        return SimpleNamespace(gates={"intent": mode})

    store.config = config
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    Verdicts("sw", "intent", tmp_path).write("t1", "fail", "missing required behavior", head="abc", url=URL)
    monkeypatch.setattr(intent, "Verdicts", lambda slug, gate, home=None: Verdicts(slug, gate, tmp_path))
    monkeypatch.setattr(intent, "pr_head", lambda url: pytest.fail("use the observed head"))
    monkeypatch.setattr(queue_exit, "require_fresh", lambda url, head: None)
    queued, events = [], []
    monkeypatch.setattr(intent.log, "append", lambda slug, row, home=None: events.append((slug, row, home)))

    def operate(action, url, admit, journal, attempts):
        assert journal.url == URL
        admit("abc")
        queued.append(url)
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", "queue", URL]) == code
    output = capsys.readouterr()
    assert ("missing required behavior" in output.err) is warned
    assert queued == ([URL] if code == 0 else [])
    if logged is None:
        assert events == []
        return
    [(slug, row, home)] = events
    assert slug == "sw"
    assert home == (None if mode == "enforce" else tmp_path)
    assert (row.gate, row.kind, row.agent, row.task, row.tool) == (
        "intent",
        logged,
        "engineer@a1b2c3-0001",
        "t1",
        "Bash",
    )
    assert row.reason.startswith("intent check failed for task t1: missing required behavior.")
    assert row.reason.endswith("then run agentihooks swarm sw pr <url> for a new check.")


def test_a_checks_wait_on_the_queued_head_resolves_once_it_is_green():
    from types import SimpleNamespace

    held = {"kind": "checks", "target": URL, "head": "old"}
    pull = SimpleNamespace(state="OPEN", head="old", resolved=False, red=False, unpassed_gate="")
    assert waits.checks_resolution(held, lambda url: pull) == ""
    pull.resolved = True
    assert waits.checks_resolution(held, lambda url: pull) == f"checks on {URL}, now green"


def test_cli_refuses_an_unknown_queue_operation(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["sw", "merge", "unknown", URL])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("base", ["main", "master", "v1"])
@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_queue_mutations_refuse_release_branches(action, base):
    run, calls = runner({"data": {"resource": {**OPEN, "baseRefName": base}}})
    with pytest.raises(SwarmError) as exc:
        merge_queue.operate(action, URL, run)
    assert str(exc.value) == "swarm merge queue operations require a pull request into dev"
    assert len(calls) == 1


@pytest.mark.parametrize("base", ["main", "master", "v1"])
def test_state_reports_a_pull_request_into_any_base(base):
    run, calls = runner({"data": {"resource": {**OPEN, "baseRefName": base}}})

    assert merge_queue.operate("state", URL, run)["state"] == "OPEN"
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [None, {}])
def test_state_rejects_a_resource_that_is_not_a_pull_request(raw):
    run, _ = runner({"data": {"resource": raw}})
    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("state", URL, run)
    assert str(exc.value) == "GitHub URL does not identify a pull request"


@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
def test_state_reports_queue_state_and_pull_request_lifecycle(state):
    run, calls = runner({"data": {"resource": {**OPEN, "state": state, "mergeQueueEntry": ENTRY}}})
    assert merge_queue.operate("state", URL, run) == {
        "url": URL,
        "state": state,
        "head": "abc",
        "queued": True,
        "entry": ENTRY,
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    "code, stderr, stdout, message",
    [
        (1, "denied\n", "", "denied"),
        (1, "", "", "GitHub API failed"),
        (0, "", '{"errors":[{"message":"denied"},{"message":"blocked"}]}', "denied; blocked"),
        (0, "", '{"data":null}', "GitHub API returned no data"),
        (0, "", "{}", "GitHub API: 'data'"),
    ],
)
def test_api_failures_remain_cli_failures(monkeypatch, capsys, code, stderr, stdout, message):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    original = merge_queue.operate
    monkeypatch.setattr(merge_queue, "operate", lambda action, url, **kwargs: original(action, url, run))
    assert cli.main(["sw", "merge", "state", URL]) == 1
    assert capsys.readouterr().err == f"swarm: {message}\n"


@pytest.mark.parametrize("error", [OSError("missing gh"), subprocess.TimeoutExpired("gh", 20)])
def test_transport_failures_are_reported(error):
    def run(command, **kwargs):
        raise error

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("state", URL, run)
    assert str(exc.value) == f"GitHub API: {error}"


def test_engineer_guidance_names_queue_state_and_dequeue_commands():
    from scripts.swarm import prompt

    text = prompt.build(
        "sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1"}, autonomy="assist"
    )
    assert "agentihooks swarm sw merge queue <pr url>" in text
    assert "agentihooks swarm sw merge state <pr url>" in text
    assert "agentihooks swarm sw merge dequeue <pr url>" in text
    assert "dequeue first" in text
    assert "then push" in text
    assert "once checks pass" in text


def test_state_runs_with_the_installed_gh_api_interface(tmp_path, monkeypatch):
    import os

    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" == api && "$2" == graphql && "$3" == -f && "$5" == -f ]]\n'
        '[[ "$6" == url=https://github.com/o/r/pull/7 ]]\n'
        'printf \'%s\\n\' \'{"data":{"resource":{"id":"PR_one","state":"OPEN",'
        '"headRefOid":"abc","baseRefName":"dev","mergeQueueEntry":null}}}\'\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert merge_queue.operate("state", URL) == {
        "url": URL,
        "state": "OPEN",
        "head": "abc",
        "queued": False,
        "entry": None,
    }
