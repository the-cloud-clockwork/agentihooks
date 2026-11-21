import json
import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

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
    assert result == {
        "url": URL,
        "displaced": [URL[:-1] + str(n) for n in (2, 3, 4, 5, 6)],
        "errors": [],
        "lab": None,
    }


class Journal:
    def __init__(self) -> None:
        self.records: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def groups(self) -> list[dict[str, Any]]:
        return []

    def write(self, *args: Any, **kwargs: Any) -> None:
        self.records.append((args, kwargs))


@pytest.mark.feature("merge-priority-reaches-lab")
def test_the_priority_journal_records_the_queue_move_and_the_lab_acknowledgement() -> None:
    queue = FakeQueue()
    ack = {"priority": ["head9"], "class": "hotfix", "expires_ms": 7}
    told: list[tuple[str, list[Any]]] = []

    def lab(head: str) -> dict[str, Any]:
        told.append((head, list(queue.mutations)))
        return ack

    journal = Journal()
    result = merge_queue.prioritize(URL, run=queue.run, journal=journal, lab=lab)
    assert result["lab"] == ack
    assert told == [("head9", queue.mutations)]
    ((args, kwargs),) = journal.records
    assert args[:3] == ("priority", URL, "head9")
    assert ["queue", URL] in kwargs["steps"]
    assert kwargs["lab"] == ack


@pytest.mark.feature("merge-priority-reaches-lab")
def test_a_lab_that_cannot_be_told_leaves_the_queue_move_standing() -> None:
    queue = FakeQueue()
    journal = Journal()
    refused = {"error": "the swarm has no lab url"}
    result = merge_queue.prioritize(URL, run=queue.run, journal=journal, lab=lambda head: refused)
    assert (result["errors"], result["lab"]) == ([], refused)
    assert journal.records[0][1]["lab"] == refused
    assert [int(e["pullRequest"]["id"][1:]) for e in queue.entries] == [1, 9, 2, 3]


@pytest.mark.feature("merge-priority-reaches-lab")
def test_a_failed_queue_move_still_tells_the_lab() -> None:
    queue = FakeQueue()

    def run(command: list[str], **kwargs: Any) -> Any:
        if "enqueuePullRequest" in " ".join(command) and "id=P9" in command:
            return subprocess.CompletedProcess(command, 1, "", "insertion refused")
        return queue.run(command, **kwargs)

    ack = {"priority": ["head9"], "class": "hotfix", "expires_ms": 7}
    told: list[str] = []

    def lab(head: str) -> dict[str, Any]:
        told.append(head)
        return ack

    journal = Journal()
    result = merge_queue.prioritize(URL, run=run, journal=journal, lab=lab)
    assert (told, result["errors"], result["lab"]) == (["head9"], ["insertion refused"], ack)
    assert journal.records[0][1]["lab"] == ack


@pytest.mark.feature("merge-priority-reaches-lab")
def test_the_swarm_tells_its_own_lab_of_a_priority_move(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    from scripts.swarm import lab_control
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.names.mint = lambda: "a1b2c3"
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1, lab_url="http://lab:8800"))
    monkeypatch.setenv(lab_control.TOKEN, "tok")
    asked: list[tuple[str, str, str]] = []

    def acknowledge(url: str, head: str, environ: Any) -> dict[str, Any]:
        asked.append((url, head, environ[lab_control.TOKEN]))
        return {"ok": True}

    def prioritize(
        url: str,
        journal: queue_record.Journal,
        displaced: Any,
        lab: Callable[[str], dict[str, Any]],
        lift: bool = False,
    ) -> dict[str, Any]:
        assert (url, journal.url) == (URL, URL)
        return {"url": url, "displaced": [], "errors": [], "lab": lab("head9")}

    monkeypatch.setattr(lab_control, "acknowledge", acknowledge)
    monkeypatch.setattr(merge_queue, "prioritize", prioritize)
    result = queue_priority.reorder(store, "sw", URL, "master", [], queue_record.Journal(URL), lift=False)
    assert result["lab"] == {"ok": True}
    assert asked == [("http://lab:8800", "head9", "tok")]


@pytest.mark.parametrize("errors, status", [([], 0), (["restore refused", "second refusal"], 1)])
def test_master_priority_notifies_displaced_owners(monkeypatch, capsys, errors, status):
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("master")
    store.config = lambda slug: SimpleNamespace(lab_url="")
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


def test_priority_stops_restoring_at_the_first_refusal_so_the_rest_keep_their_order():
    queue = FakeQueue()

    def run(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P2" in command:
            return subprocess.CompletedProcess(command, 1, "", "restore refused")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run)
    assert result == {
        "url": URL,
        "displaced": [],
        "errors": [f"could not restore {URL[:-1]}2: restore refused"],
        "lab": None,
    }
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9"]


def test_priority_requeues_a_queued_target_ejected_before_a_dequeue_fails():
    queue = FakeQueue(queued=True)

    def run(command, **kwargs):
        dequeue = "dequeuePullRequest" in " ".join(command)
        if dequeue and "id=P3" in command:
            return subprocess.CompletedProcess(command, 1, "", "dequeue refused")
        done = queue.run(command, **kwargs)
        if dequeue and "id=P4" in command:
            queue.entries = [e for e in queue.entries if e["pullRequest"]["id"] != "P9"]
        return done

    result = merge_queue.prioritize(URL, run=run)
    assert result == {"url": URL, "displaced": [URL[:-1] + "4"], "errors": ["dequeue refused"], "lab": None}
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P2", "P3", "P9", "P4"]


@pytest.mark.parametrize("queued, split", [(False, 2), (True, 4)])
def test_priority_preserves_order_across_queue_pages(queued, split):
    queue = FakeQueue()
    queue.entries += ([queue.entry(9)] if queued else []) + [queue.entry(4), queue.entry(5)]
    pages = []

    def run(command, **kwargs):
        if command[2] == "graphql" and "entries(" in command[4]:
            args = dict(item.split("=", 1) for item in command[4::2])
            assert args.pop("url") == URL
            args.pop("query")
            if not pages:
                assert args == {}
                nodes = queue.entries[:split]
                info = {"hasNextPage": True, "endCursor": "cursor"}
            else:
                assert args == {"after": "cursor"}
                nodes = queue.entries[split:]
                info = {"hasNextPage": False, "endCursor": None}
            pages.append(nodes.copy())
            data = {
                "data": {"resource": {"repository": {"mergeQueue": {"entries": {"nodes": nodes, "pageInfo": info}}}}}
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run)
    assert len(pages) == 2
    assert result == {
        "url": URL,
        "displaced": [URL[:-1] + n for n in ("2", "3", "4", "5")],
        "errors": [],
        "lab": None,
    }
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P9", "P2", "P3", "P4", "P5"]


def test_priority_does_not_remove_an_entry_that_started_running():
    queue = FakeQueue()

    def run(command, **kwargs):
        done = queue.run(command, **kwargs)
        if command[2] == "graphql" and "entries(" in command[4]:
            queue.entries[1]["state"] = "AWAITING_CHECKS"
        return done

    displaced = Displaced(queue)
    result = merge_queue.prioritize(URL, run=run, displaced=displaced)
    assert result == {"url": URL, "displaced": [URL[:-1] + "3"], "errors": [], "lab": None}
    assert queue.mutations == [("dequeue", 3), ("queue", 9), ("queue", 3)]
    assert [e["pullRequest"]["id"] for e in queue.entries] == ["P1", "P2", "P9", "P3"]
    assert displaced.pulls == {}


def test_priority_checks_each_restored_entry_with_an_empty_body_when_it_has_none(monkeypatch):
    queue, checked = FakeQueue(), []
    monkeypatch.setattr(
        merge_queue.staged_compatibility, "require", lambda url, head, body, run: checked.append((url, head, body))
    )
    merge_queue.prioritize(URL, run=queue.run)
    assert checked == [(URL[:-1] + str(n), f"head{n}", "") for n in (9, 2, 3)]


@pytest.mark.feature("merge-priority-moves-waiting-entries-at-once")
def test_master_priority_moves_waiting_entries_without_cancelling_running_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.swarm.test_merge_queue import swarm_of

    store = swarm_of("master")
    store.config = lambda slug: SimpleNamespace(lab_url="")
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(tasks=lambda slug: []))
    queue = FakeQueue(queued=True)
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
    runs = []
    original_rest = merge_queue.rest

    def rest(path: str, run: Callable[..., Any]) -> Any:
        if "/actions/" not in path:
            return original_rest(path, run)
        runs.append(path)
        return {"workflow_runs": [{"id": 42, "status": "in_progress", "head_sha": "group-head"}]}

    monkeypatch.setattr(merge_queue, "rest", rest)
    assert cli.main(["sw", "merge", "priority", URL]) == 0
    assert queue.mutations == [("dequeue", n) for n in (4, 3, 2)] + [("queue", n) for n in (2, 3, 4)]
    assert [(entry["pullRequest"]["id"], entry["state"]) for entry in queue.entries] == [
        ("P1", "AWAITING_CHECKS"),
        ("P9", "QUEUED"),
        ("P2", "QUEUED"),
        ("P3", "QUEUED"),
        ("P4", "QUEUED"),
    ]
    assert json.loads(capsys.readouterr().out)["displaced"] == [URL[:-1] + str(n) for n in (2, 3, 4)]
    assert store.redis.hgetall(store.key("sw", "queue-priority")) == {}
    assert runs == []
    assert [args[0] for args, _ in records] == ["priority"]


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


@pytest.mark.parametrize("refused, kept", [("", []), ("P2", [URL[:-1] + "2", URL[:-1] + "3"])])
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
