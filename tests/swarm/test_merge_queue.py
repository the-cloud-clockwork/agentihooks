import json
import subprocess

import pytest

from scripts.swarm import cli, merge_queue, waits
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/7"
OPEN = {"id": "PR_one", "state": "OPEN", "headRefOid": "abc", "baseRefName": "dev", "mergeQueueEntry": None}
ENTRY = {"id": "MQ_one", "position": 2, "state": "AWAITING_CHECKS"}
AUTHORED = {"sha": "abc", "commit": {"message": "Change the code"}, "committer": {"login": "nestorcolt"}}
REFRESHED = {"sha": "abc", "commit": {"message": "Merge branch 'dev' into ci-1"}, "committer": {"login": "web-flow"}}


def runner(*responses):
    calls = []
    pending = iter(responses)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        response = next(pending)
        assert command[:2] == ["gh", "api"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20}
        if command[2] == "graphql":
            assert command[3] == "-f"
            assert command[4] in [
                f"query={query}"
                for query in (merge_queue.STATE, merge_queue.FRESHNESS, merge_queue.QUEUE, merge_queue.DEQUEUE)
            ]
            if command[4] in (f"query={merge_queue.STATE}", f"query={merge_queue.FRESHNESS}"):
                assert command[5:] == ["-f", f"url={URL}"]
        elif isinstance(response, str):
            assert command == ["gh", "api", "repos/o/r/actions/jobs/31/logs"]
        elif "workflow_runs" in response:
            assert command == [
                "gh",
                "api",
                "repos/o/r/actions/workflows/test.yml/runs?event=pull_request&head_sha=abc&per_page=1",
            ]
        elif "jobs" in response:
            assert command == ["gh", "api", "repos/o/r/actions/runs/21/jobs?per_page=100"]
        elif "commit" in response:
            assert command == ["gh", "api", "repos/o/r/commits/abc"]
        elif "files" in response:
            assert command == ["gh", "api", "repos/o/r/compare/cccccccccccccccccccccccccccccccccccccccc...current"]
        elif "message" in response:
            assert command == [
                "gh",
                "api",
                "repos/o/r/pulls/12/update-branch",
                "--method",
                "PUT",
                "-f",
                "expected_head_sha=abc",
            ]
        return subprocess.CompletedProcess(command, 0, response if isinstance(response, str) else json.dumps(response))

    return run, calls


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
        {
            "data": {
                "resource": {"number": 12, "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "c" * 40}}}}
            }
        },
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": "success"}]},
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        "2026-10-09T09:20:05Z setup\n2026-10-09T09:20:06Z   BASE: cccccccccccccccccccccccccccccccccccccccc\n2026-10-09T09:20:07Z done\n",
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "c" * 40}}}}}},
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
    command, _ = calls[6]
    assert command[:3] == ["gh", "api", "graphql"]
    assert "enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head})" in command[4]
    assert command[5:] == ["-f", "id=PR_one", "-f", "head=abc"]
    assert calls[-1][0] == calls[0][0]
    assert not any("compare/" in str(call[0]) for call in calls)


@pytest.mark.parametrize(
    "changed",
    [
        [{"filename": ".github/workflows/test.yml"}],
        [{"filename": "scripts/ci_mutation/__init__.py"}],
        [{"filename": "scripts/check_gate.py"}],
        [{"filename": "tests/swarm/test_tick.py"}],
        [{"filename": "docs/old.py", "previous_filename": "tests/old.py"}],
        [{"filename": f"hooks/item{i}.py"} for i in range(300)],
    ],
)
@pytest.mark.parametrize(
    "head",
    [
        AUTHORED,
        {**AUTHORED, "commit": {"message": "Merge branch 'dev' into ci-1"}},
        {**REFRESHED, "commit": {"message": "Merge pull request #3 from o/side"}},
        {**REFRESHED, "committer": None},
    ],
)
def test_queue_refreshes_changed_grading_inputs_before_enqueueing(changed, head):
    run, calls = runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {
                    "number": 12,
                    "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "current"}}},
                }
            }
        },
        {
            "workflow_runs": [
                {
                    "name": "Tests",
                    "head_sha": "abc",
                    "conclusion": "success",
                    "id": 21,
                }
            ]
        },
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        "2026-10-09T09:20:05Z setup\n2026-10-09T09:20:06Z   BASE: cccccccccccccccccccccccccccccccccccccccc\n2026-10-09T09:20:07Z done\n",
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        head,
        {"files": changed, "total_commits": 1},
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        {"message": "Updating pull request branch."},
        {"data": {"resource": {**OPEN, "headRefOid": "updated"}}},
    )
    result = merge_queue.operate("queue", URL, run)
    assert result["queued"] is False
    assert result["head"] == "updated"
    assert result["waiting"] == "checks"
    assert result["previous_head"] == "abc"
    assert any("repos/o/r/pulls/12/update-branch" in call[0] for call in calls)
    assert not any("enqueuePullRequest(input:" in str(call[0]) for call in calls)


def test_queue_does_not_refresh_unrelated_dev_changes():
    run, calls = runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {
                    "number": 12,
                    "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "current"}}},
                }
            }
        },
        {
            "workflow_runs": [
                {
                    "name": "Tests",
                    "head_sha": "abc",
                    "conclusion": "success",
                    "id": 21,
                }
            ]
        },
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        "2026-10-09T09:20:05Z setup\n2026-10-09T09:20:06Z   BASE: cccccccccccccccccccccccccccccccccccccccc\n2026-10-09T09:20:07Z done\n",
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        AUTHORED,
        {"files": [{"filename": "scripts/swarm/intent.py"}], "total_commits": 1},
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )
    assert merge_queue.operate("queue", URL, run)["queued"] is True
    assert any("repos/o/r/compare/cccccccccccccccccccccccccccccccccccccccc...current" in call[0] for call in calls)
    assert not any("update-branch" in str(call[0]) for call in calls)


def refreshed_head_runner(conclusion, *rest):
    return runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {
                    "number": 12,
                    "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "current"}}},
                }
            }
        },
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": conclusion}]},
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        "2026-10-09T09:20:05Z setup\n2026-10-09T09:20:06Z   BASE: cccccccccccccccccccccccccccccccccccccccc\n2026-10-09T09:20:07Z done\n",
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        *rest,
    )


def test_queue_enqueues_a_green_refreshed_head_although_dev_moved_again():
    run, calls = refreshed_head_runner(
        "success",
        REFRESHED,
        {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}},
        {"data": {"resource": {**OPEN, "mergeQueueEntry": ENTRY}}},
    )
    result = merge_queue.operate("queue", URL, run)
    assert result["queued"] is True
    assert "waiting" not in result
    assert calls[6][0] == ["gh", "api", "repos/o/r/commits/abc"]
    assert calls[7][0][5:] == ["-f", "id=PR_one", "-f", "head=abc"]
    assert not any("compare/" in str(call[0]) or "update-branch" in str(call[0]) for call in calls)


@pytest.mark.parametrize("conclusion", [None, "failure"])
def test_queue_still_blocks_a_red_refreshed_head(conclusion):
    run, calls = refreshed_head_runner(conclusion)
    with pytest.raises(SwarmError, match="^the current pull request head must pass Tests before queueing$"):
        merge_queue.operate("queue", URL, run)
    assert not any("enqueuePullRequest(input:" in str(call[0]) or "update-branch" in str(call[0]) for call in calls)


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled"])
def test_queue_waits_for_green_checks_on_the_current_head(conclusion):
    run, calls = runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {
                    "number": 12,
                    "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "current"}}},
                }
            }
        },
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": conclusion}]},
    )
    with pytest.raises(SwarmError, match="^the current pull request head must pass Tests before queueing$"):
        merge_queue.operate("queue", URL, run)
    assert not any("update-branch" in str(call[0]) or "enqueuePullRequest(input:" in str(call[0]) for call in calls)


@pytest.mark.parametrize("log", ["", "2026-10-09T09:20:06Z   BASE: origin/dev\n"])
def test_queue_refuses_an_unknown_checked_base(log):
    run, calls = runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {
                    "number": 12,
                    "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "current"}}},
                }
            }
        },
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": "success"}]},
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        log,
    )
    with pytest.raises(SwarmError, match="^cannot establish the base of the green checks$"):
        merge_queue.operate("queue", URL, run)
    assert not any("enqueuePullRequest(input:" in str(call[0]) for call in calls)


def test_cli_registers_a_checks_wait_after_refreshing(monkeypatch, capsys):
    store = swarm_of("eng")
    store.redis = object()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(
        merge_queue, "operate", lambda action, url: {"queued": False, "waiting": "checks", "previous_head": "old"}
    )
    monkeypatch.setattr(cli, "now_ms", lambda: 1000)
    monkeypatch.setattr(waits, "CHECKED_MINUTES", 2)
    seen = []
    monkeypatch.setattr(cli.idle, "declare_wait", lambda *args, **kwargs: seen.append((args, kwargs)))
    assert cli.main(["sw", "merge", "queue", URL]) == 0
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args[:3] == (store.redis, "sw", "engineer@a1b2c3-0001")
    assert kwargs["on"] == {"kind": "checks", "target": URL, "previous_head": "old"}
    assert args[3:] == (121000, "dev changed grading inputs; branch updated", 1000)
    assert json.loads(capsys.readouterr().out)["waiting"] == "checks"


def test_refresh_wait_does_not_resolve_the_old_green_head():
    from types import SimpleNamespace

    held = {"kind": "checks", "target": URL, "previous_head": "old"}
    pull = SimpleNamespace(state="OPEN", head="old", resolved=True, red=False, unpassed_gate="")
    assert waits.checks_resolution(held, lambda url: pull) == ""
    assert waits.checks_resolution(held, lambda url: pull) == ""
    pull.head = "updated"
    pull.resolved = False
    assert waits.checks_resolution(held, lambda url: pull) == ""
    assert waits.checks_resolution(held, lambda url: pull) == ""
    pull.resolved = True
    assert waits.checks_resolution(held, lambda url: pull) == f"checks on {URL}, now green"


def test_queue_refuses_dev_advancing_during_comparison():
    run, calls = runner(
        {"data": {"resource": OPEN}},
        {
            "data": {
                "resource": {"number": 12, "repository": {"nameWithOwner": "o/r", "ref": {"target": {"oid": "c" * 40}}}}
            }
        },
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": "success"}]},
        {"jobs": [{"id": 31, "name": "test-count (3.11)", "conclusion": "success"}]},
        "2026-10-09T09:20:05Z setup\n2026-10-09T09:20:06Z   BASE: cccccccccccccccccccccccccccccccccccccccc\n2026-10-09T09:20:07Z done\n",
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "current"}}}}}},
        AUTHORED,
        {"files": [{"filename": "scripts/swarm/intent.py"}]},
        {"data": {"resource": {"repository": {"ref": {"target": {"oid": "newer"}}}}}},
    )
    with pytest.raises(SwarmError, match="^dev advanced during the grading comparison; retry queueing$"):
        merge_queue.operate("queue", URL, run)
    assert not any("enqueuePullRequest(input:" in str(call[0]) for call in calls)


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"id": 31, "name": "wrong job", "conclusion": "success"}],
        [{"id": 31, "name": "test-count (3.11)", "conclusion": "failure"}],
    ],
)
def test_queue_requires_the_successful_provenance_job(jobs):
    run, _ = runner(
        {"data": {"resource": OPEN}},
        {"data": {"resource": {"number": 12, "repository": {"nameWithOwner": "o/r"}}}},
        {"workflow_runs": [{"id": 21, "head_sha": "abc", "conclusion": "success"}]},
        {"jobs": jobs},
    )
    with pytest.raises(SwarmError, match="^cannot establish the base of the green checks$"):
        merge_queue.operate("queue", URL, run)


@pytest.mark.parametrize("records", [[], [{"id": 21, "head_sha": "older", "conclusion": "success"}]])
def test_queue_requires_a_run_for_its_observed_head(records):
    run, _ = runner(
        {"data": {"resource": OPEN}},
        {"data": {"resource": {"number": 12, "repository": {"nameWithOwner": "o/r"}}}},
        {"workflow_runs": records},
    )
    with pytest.raises(SwarmError, match="^the current pull request head must pass Tests before queueing$"):
        merge_queue.operate("queue", URL, run)


@pytest.mark.parametrize("stderr, message", [("denied\n", "denied"), ("", "GitHub API failed")])
def test_queue_reports_rest_api_failure(stderr, message):
    initial, calls = runner(
        {"data": {"resource": OPEN}},
        {"data": {"resource": {"number": 12, "repository": {"nameWithOwner": "o/r"}}}},
    )

    def run(command, **kwargs):
        if len(calls) < 2:
            return initial(command, **kwargs)
        return subprocess.CompletedProcess(command, 1, "", stderr)

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("queue", URL, run)
    assert str(exc.value) == message


@pytest.mark.parametrize("error", [OSError("missing gh"), subprocess.TimeoutExpired("gh", 20)])
def test_queue_reports_rest_transport_failure(error):
    initial, calls = runner(
        {"data": {"resource": OPEN}},
        {"data": {"resource": {"number": 12, "repository": {"nameWithOwner": "o/r"}}}},
    )

    def run(command, **kwargs):
        if len(calls) < 2:
            return initial(command, **kwargs)
        raise error

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("queue", URL, run)
    assert str(exc.value) == f"GitHub API: {error}"


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

    agent = SimpleNamespace(name="engineer@a1b2c3-0001", lane=lane, task="t1")
    names = SimpleNamespace(swarm_slug=lambda slug: slug, resolve=lambda name: name)
    return SimpleNamespace(names=names, agents=lambda slug: [agent])


@pytest.mark.parametrize("action", ["queue", "dequeue", "state"])
def test_cli_routes_each_operation_and_prints_its_state(monkeypatch, capsys, action):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    seen = []

    def operate(verb, url):
        seen.append((verb, url))
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", action, URL]) == 0
    assert seen == [(action, URL)]
    assert capsys.readouterr().out == '{"queued": true}\n'


@pytest.mark.parametrize("action", ["queue", "dequeue"])
def test_cli_refuses_the_master_a_queue_change(monkeypatch, capsys, action):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("master"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url: pytest.fail("the master changed the queue"))

    assert cli.main(["sw", "merge", action, URL]) == 1
    assert capsys.readouterr().err == "swarm: engineer@a1b2c3-0001 is the swarm master; the master works no task\n"


def test_cli_lets_the_master_read_queue_state(monkeypatch, capsys):
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("master"))
    monkeypatch.setattr(merge_queue, "operate", lambda verb, url: {"queued": False})

    assert cli.main(["sw", "merge", "state", URL]) == 0
    assert capsys.readouterr().out == '{"queued": false}\n'


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
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "connect", lambda: SimpleNamespace(names=SimpleNamespace(swarm_slug=lambda s: s)))

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    original = merge_queue.operate
    monkeypatch.setattr(merge_queue, "operate", lambda action, url: original(action, url, run))
    assert cli.main(["sw", "merge", "state", URL]) == 1
    assert capsys.readouterr().err == f"swarm: {message}\n"


@pytest.mark.parametrize("error", [OSError("missing gh"), subprocess.TimeoutExpired("gh", 20)])
def test_transport_failures_are_reported(error):
    def run(command, **kwargs):
        raise error

    with pytest.raises(SwarmError) as exc:
        merge_queue.operate("state", URL, run)
    assert str(exc.value) == f"GitHub API: {error}"


@pytest.mark.parametrize("autonomy", ["full", "assist"])
def test_engineer_guidance_names_queue_state_and_dequeue_commands(autonomy):
    from scripts.swarm import prompt

    text = prompt.build(
        "sw", "/repo", "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "phase": "p1"}, autonomy=autonomy
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
