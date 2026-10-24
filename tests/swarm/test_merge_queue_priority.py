import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, ledger_events, merge_queue, queue_priority, queue_record

pytestmark = pytest.mark.unit
URL = "https://github.com/o/r/pull/9"
READ_PULL = ledger_events.view


@pytest.fixture(autouse=True)
def read_required_checks(monkeypatch):
    monkeypatch.setattr(ledger_events, "view", READ_PULL)


class FakeQueue:
    def __init__(self, queued=False):
        self.entries = [self.entry(1, "AWAITING_CHECKS"), self.entry(2), self.entry(3)]
        if queued:
            self.entries += [self.entry(9), self.entry(4)]
        self.mutations = []

    def entry(self, number, state="QUEUED"):
        return {
            "id": f"E{number}",
            "state": state,
            "pullRequest": {"id": f"P{number}", "url": URL[:-1] + str(number), "headRefOid": f"head{number}"},
        }

    def run(self, command, **kwargs):
        if command[2] != "graphql":
            assert command == ["gh", "api", "repos/o/r/rules/branches/dev"]
            data = [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "Gate — Required"}]},
                }
            ]
        else:
            args = dict(item.split("=", 1) for item in command[4::2])
            query = args.pop("query")
            if query == ledger_events.PULL_QUERY.replace("$number", "9"):
                assert args == {"url": URL}
                data = {
                    "resource": {
                        "state": "OPEN",
                        "headRefOid": "head9",
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {
                                                        "name": "Gate — Required",
                                                        "isRequired": True,
                                                        "conclusion": "SUCCESS",
                                                    }
                                                ],
                                                "pageInfo": {"hasNextPage": False},
                                            }
                                        },
                                        "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                                    }
                                }
                            ]
                        },
                    }
                }
            elif "enqueuePullRequest" in query:
                assert query == merge_queue.QUEUE
                assert set(args) == {"id", "head"}
                number = int(args["id"][1:])
                assert args["head"] == f"head{number}"
                self.entries.append(self.entry(number))
                self.mutations.append(("queue", number))
                data = {"enqueuePullRequest": {"mergeQueueEntry": {"id": f"E{number}"}}}
            elif "dequeuePullRequest" in query:
                assert query == merge_queue.DEQUEUE
                assert set(args) == {"id"}
                number = int(args["id"][1:])
                self.entries = [e for e in self.entries if e["pullRequest"]["id"] != args["id"]]
                self.mutations.append(("dequeue", number))
                data = {"dequeuePullRequest": {"mergeQueueEntry": {"id": f"E{number}"}}}
            elif "entries(" in query:
                assert args == {"url": URL}
                data = {
                    "resource": {
                        "repository": {
                            "mergeQueue": {
                                "entries": {
                                    "nodes": self.entries.copy(),
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                }
            else:
                assert query in (merge_queue.STATE, merge_queue.FRESHNESS)
                assert set(args) == {"url"}
                number = int(args["url"].rsplit("/", 1)[1])
                entry = next((e for e in self.entries if e["pullRequest"]["id"] == f"P{number}"), None)
                data = {
                    "resource": {
                        "id": f"P{number}",
                        "state": "OPEN",
                        "headRefOid": f"head{number}",
                        "baseRefName": "dev",
                        "mergeQueueEntry": entry,
                        "repository": {"nameWithOwner": "o/r", "mergeQueue": {"id": "Q"}},
                    }
                }
            data = {"data": data}
        return subprocess.CompletedProcess(command, 0, json.dumps(data), "")


@pytest.mark.parametrize("queued, expected", [(False, [1, 9, 2, 3]), (True, [1, 9, 2, 3, 4])])
def test_priority_preserves_displaced_order_and_running_entries(queued, expected):
    queue = FakeQueue(queued)
    result = merge_queue.prioritize(URL, run=queue.run)
    assert [int(e["pullRequest"]["id"][1:]) for e in queue.entries] == expected
    later = [4] if queued else []
    assert queue.mutations == [("dequeue", n) for n in [*later, 3, 2]] + ([] if queued else [("queue", 9)]) + [
        ("queue", n) for n in [2, 3, *later]
    ]
    assert result["displaced"] == [URL[:-1] + str(n) for n in [2, 3, *later]]


def test_priority_keeps_displaced_ahead_of_later_arrivals_in_one_pass():
    queue = FakeQueue(queued=True)
    queue.entries += [queue.entry(5), queue.entry(6)]
    result = merge_queue.prioritize(URL, run=queue.run)
    assert [int(e["pullRequest"]["id"][1:]) for e in queue.entries] == [1, 9, 2, 3, 4, 5, 6]
    assert queue.mutations == [("dequeue", n) for n in (6, 5, 4, 3, 2)] + [("queue", n) for n in (2, 3, 4, 5, 6)]
    assert result == {"url": URL, "displaced": [URL[:-1] + str(n) for n in (2, 3, 4, 5, 6)], "errors": []}


@pytest.mark.parametrize("errors, status", [([], 0), (["restore refused", "second refusal"], 1)])
def test_master_priority_notifies_displaced_owners(monkeypatch, capsys, errors, status):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("master")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    tasks = [
        {"pr_url": URL[:-1] + "2", "claimed_by": "ci@owner"},
        {"pr_url": URL[:-1] + "3", "claimed_by": ""},
        {"pr_url": URL[:-1] + "4", "claimed_by": "ci@unrelated"},
        {"pr_url": URL[:-1] + "5", "claimed_by": "ci@released", "state": "pr", "awaiting": "merge"},
        {"pr_url": URL[:-1] + "6", "claimed_by": "ci@done", "state": "done"},
    ]

    def ledger_tasks(slug):
        assert slug == "sw"
        return tasks

    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=ledger_tasks))

    def prioritize(url, journal, **kwargs):
        assert url == URL
        assert isinstance(journal, queue_record.Journal) and journal.url == URL
        return {"url": url, "displaced": [URL[:-1] + str(n) for n in (2, 3, 5, 6)], "errors": errors}

    monkeypatch.setattr(merge_queue, "prioritize", prioritize)
    monkeypatch.setattr(queue_record.Journal, "groups", lambda self: [])
    sent = []

    def inbox(redis):
        assert redis is store.redis
        return SimpleNamespace(
            send=lambda sender, recipient, text, **flags: sent.append((sender, recipient, text, flags))
        )

    monkeypatch.setattr(queue_priority, "InboxStore", inbox)
    assert cli.main(["sw", "merge", "priority", URL]) == status
    assert sent == [
        (
            "engineer@a1b2c3-0001",
            "ci@owner",
            f"Your pull request {URL[:-1]}2 was requeued in its original order after priority request {URL}.",
            {"fyi": True},
        )
    ]
    output = capsys.readouterr()
    if errors:
        assert output.err == "swarm: restore refused; second refusal\n"
    else:
        assert json.loads(output.out)["displaced"] == [URL[:-1] + str(n) for n in (2, 3, 5, 6)]


def test_worker_cannot_prioritize(monkeypatch, capsys):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("ci")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(merge_queue, "prioritize", lambda url, journal: pytest.fail("worker reordered queue"))
    assert cli.main(["sw", "merge", "priority", URL]) == 1
    assert capsys.readouterr().err == "swarm: only the master or the operator prioritizes the merge queue\n"


@pytest.mark.parametrize("failure", ["critical", "dequeue", "timeout"])
def test_priority_restores_entries_after_a_mutation_fails(failure):
    queue = FakeQueue()

    def run(command, **kwargs):
        if ("enqueuePullRequest" in " ".join(command) and "id=P9" in command and failure == "critical") or (
            "dequeuePullRequest" in " ".join(command) and "id=P2" in command and failure == "dequeue"
        ):
            return subprocess.CompletedProcess(command, 1, "", "insertion refused")
        done = queue.run(command, **kwargs)
        if "dequeuePullRequest" in " ".join(command) and "id=P2" in command and failure == "timeout":
            raise subprocess.TimeoutExpired(command, 20)
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert len(result["errors"]) == 1
    assert "timed out" in result["errors"][0] if failure == "timeout" else result["errors"] == ["insertion refused"]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P2", "P3"]


@pytest.mark.parametrize("base, queue_exists", [("main", True), ("dev", False)])
def test_priority_refuses_release_targets_and_missing_queues(base, queue_exists):
    from scripts.swarm.store import SwarmError

    queue = FakeQueue()

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource")
        if raw and "baseRefName" in raw:
            raw["baseRefName"] = base
            if not queue_exists:
                raw["repository"]["mergeQueue"] = None
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    with pytest.raises(SwarmError) as error:
        merge_queue.prioritize(URL, run=run)
    expected = (
        "queue priority requires an open pull request into dev"
        if base == "main"
        else "queue priority requires a dev merge queue"
    )
    assert str(error.value) == expected
    assert queue.mutations == []


def test_priority_reenqueues_a_critical_entry_ejected_during_reorder():
    queue = FakeQueue(queued=True)

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        if "dequeuePullRequest" in " ".join(command) and "id=P2" in command:
            queue.entries = [e for e in queue.entries if e["pullRequest"]["id"] != "P9"]
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert result["errors"] == []
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9", "P2", "P3", "P4"]


def test_priority_attempts_every_restoration_after_one_is_refused():
    queue = FakeQueue()

    def run(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P2" in command:
            return subprocess.CompletedProcess(command, 1, "", "restore refused")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run)
    assert result["displaced"] == [URL[:-1] + "3"]
    assert "could not restore" in result["errors"][0]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9", "P3"]


def test_priority_preserves_order_across_queue_pages():
    queue = FakeQueue()
    queue.entries += [queue.entry(4), queue.entry(5)]
    pages = []

    def run(command, **kwargs):
        if command[2] == "graphql" and "entries(" in command[4]:
            args = dict(item.split("=", 1) for item in command[4::2])
            assert args.pop("url") == URL
            args.pop("query")
            if not pages:
                assert args == {}
                nodes = queue.entries[:2]
                info = {"hasNextPage": True, "endCursor": "cursor"}
            else:
                assert args == {"after": "cursor"}
                nodes = queue.entries[2:]
                info = {"hasNextPage": False, "endCursor": None}
            pages.append(nodes.copy())
            data = {
                "data": {"resource": {"repository": {"mergeQueue": {"entries": {"nodes": nodes, "pageInfo": info}}}}}
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run)
    assert len(pages) == 2
    assert result == {"url": URL, "displaced": [URL[:-1] + n for n in ("2", "3", "4", "5")], "errors": []}
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9", "P2", "P3", "P4", "P5"]


def test_priority_does_not_remove_an_entry_that_started_running():
    queue = FakeQueue()

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        if command[2] == "graphql" and "entries(" in command[4]:
            queue.entries[1]["state"] = "AWAITING_CHECKS"
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert result == {"url": URL, "displaced": [URL[:-1] + "3"], "errors": []}
    assert queue.mutations == [("dequeue", 3), ("queue", 9), ("queue", 3)]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P2", "P9", "P3"]


def test_master_priority_defers_without_cancelling_running_head(monkeypatch, capsys):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("master")
    store.config = lambda slug: None
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: []))
    queue = FakeQueue()
    prioritize = merge_queue.prioritize
    monkeypatch.setattr(
        merge_queue,
        "prioritize",
        lambda url, journal, **kwargs: prioritize(url, run=queue.run, journal=journal, **kwargs),
    )
    groups = [{"position": 1, "head": "group-head", "pull": URL[:-1] + "1"}]
    records = []
    monkeypatch.setattr(queue_record.Journal, "groups", lambda self: groups)
    monkeypatch.setattr(queue_record.Journal, "write", lambda self, *args, **kwargs: records.append((args, kwargs)))

    original_rest = merge_queue.rest

    def rest(path, run):
        if "/actions/" not in path:
            return original_rest(path, run)
        assert path == "repos/o/r/actions/workflows/test.yml/runs?event=merge_group&head_sha=group-head&per_page=100"
        return {"workflow_runs": [{"id": 42, "status": "in_progress", "head_sha": "group-head"}]}

    monkeypatch.setattr(merge_queue, "rest", rest)
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert queue.mutations == []
    assert json.loads(capsys.readouterr().out)["deferred"] is True
    pending = store.redis.hgetall(store.key("sw", "queue-priority"))
    assert len(pending) == 1
    assert json.loads(pending[URL])["run_id"] == 42
    assert records[0][0][0] == "priority-deferred"


class Displaced:
    def __init__(self, queue):
        self.queue, self.pulls = queue, {}

    def add(self, pull):
        assert any(e["pullRequest"]["id"] == pull["id"] for e in self.queue.entries)
        self.pulls[pull["url"]] = pull

    def discard(self, url):
        self.pulls.pop(url, None)


class Killed(BaseException):
    pass


def test_priority_records_each_entry_before_it_leaves_the_queue():
    queue = FakeQueue()
    displaced = Displaced(queue)

    def run(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P9" in command:
            raise Killed
        return queue.run(command, **kwargs)

    with pytest.raises(Killed):
        merge_queue.prioritize(URL, run=run, displaced=displaced)
    assert sorted(displaced.pulls) == [URL[:-1] + "2", URL[:-1] + "3"]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1"]


@pytest.mark.parametrize("refused, kept", [("", []), ("P2", [URL[:-1] + "2"])])
def test_priority_clears_each_entry_once_it_is_back(refused, kept):
    queue = FakeQueue()
    displaced = Displaced(queue)

    def run(command, **kwargs):
        if refused and "enqueuePullRequest" in " ".join(command) and f"id={refused}" in command:
            return subprocess.CompletedProcess(command, 1, "", "restore refused")
        return queue.run(command, **kwargs)

    merge_queue.prioritize(URL, run=run, displaced=displaced)
    assert sorted(displaced.pulls) == kept


def test_priority_refuses_a_draft_before_any_entry_leaves():
    from scripts.swarm.store import SwarmError

    queue = FakeQueue()

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        response = json.loads(done.stdout)
        raw = response["data"].get("resource") if command[2] == "graphql" else None
        if raw and raw.get("id") == "P9":
            raw["isDraft"] = True
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    with pytest.raises(SwarmError) as error:
        merge_queue.prioritize(URL, run=run)
    assert str(error.value) == "queue priority requires a ready pull request"
    assert queue.mutations == []
