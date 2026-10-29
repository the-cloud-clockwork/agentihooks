import json
import subprocess
from datetime import UTC, datetime

import pytest

from scripts.swarm import ledger_events, merge_queue, queue_record
from scripts.swarm.store import SwarmError
from tests.swarm.test_merge_queue_priority import URL, FakeQueue

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 16, 1, 16, 43, tzinfo=UTC)
READ_PULL = ledger_events.view


@pytest.fixture(autouse=True)
def read_required_checks(monkeypatch):
    monkeypatch.setattr(ledger_events, "view", READ_PULL)


def node(number, head, position, *, jump=False, enqueued="2026-10-16T01:00:00Z", base="b0"):
    return {
        "position": position,
        "state": "QUEUED",
        "jump": jump,
        "enqueuedAt": enqueued,
        "enqueuer": {"login": "nestorcolt"},
        "headCommit": {"oid": head},
        "baseCommit": {"oid": base},
        "pullRequest": {"number": number, "url": URL[:-1] + str(number), "headRefOid": f"pr{number}"},
    }


class Snapshots:
    def __init__(self, *pages, fail=None):
        self.pages = list(pages)
        self.fail = fail
        self.calls = []

    def __call__(self, command, **kwargs):
        args = dict(item.split("=", 1) for item in command[4::2])
        assert args.pop("query") == queue_record.GROUPS
        self.calls.append(args)
        if self.fail:
            return subprocess.CompletedProcess(command, 1, "", self.fail)
        nodes, info = self.pages.pop(0)
        data = {
            "viewer": {"login": "tcc-main-ci[bot]"},
            "resource": {"repository": {"mergeQueue": {"entries": {"nodes": nodes, "pageInfo": info}}}},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": data}), "")


LAST = {"hasNextPage": False, "endCursor": None}


def journal(tmp_path, run, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@d44a5a-1")
    return queue_record.Journal(URL, run=run, clock=lambda: NOW)


def written(tmp_path):
    return [json.loads(line) for line in (tmp_path / "merge-queue-events.jsonl").read_text().splitlines()]


def test_groups_come_back_in_queue_position_order(tmp_path, monkeypatch):
    run = Snapshots(([node(2, "h2", 2), node(1, "h1", 1)], LAST))
    assert [g["position"] for g in journal(tmp_path, run, monkeypatch).groups()] == [1, 2]


def test_an_unwritable_journal_keeps_the_record_and_reports_on_stderr(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    book = journal(blocker, Snapshots(([node(1, "h1", 1)], LAST), ([node(1, "h1", 1)], LAST)), monkeypatch)
    record = book.write("queue", URL, "head9", book.groups(), book.groups())
    assert record["rebuilt"] == []
    assert "merge queue record not written" in capsys.readouterr().err


def test_groups_read_every_queue_page_with_heads_bases_jump_and_enqueuer(tmp_path, monkeypatch):
    run = Snapshots(
        ([node(1, "h1", 1)], {"hasNextPage": True, "endCursor": "c"}), ([node(2, "h2", 2, jump=True)], LAST)
    )
    groups = journal(tmp_path, run, monkeypatch).groups()
    assert run.calls == [{"url": URL}, {"url": URL, "after": "c"}]
    assert groups == [
        {
            "pull": URL[:-1] + "1",
            "pull_head": "pr1",
            "position": 1,
            "state": "QUEUED",
            "jump": False,
            "enqueued_at": "2026-10-16T01:00:00Z",
            "enqueuer": "nestorcolt",
            "head": "h1",
            "base": "b0",
        },
        {
            "pull": URL[:-1] + "2",
            "pull_head": "pr2",
            "position": 2,
            "state": "QUEUED",
            "jump": True,
            "enqueued_at": "2026-10-16T01:00:00Z",
            "enqueuer": "nestorcolt",
            "head": "h2",
            "base": "b0",
        },
    ]


def test_a_record_names_action_actor_agent_pull_and_the_old_and_new_group_heads(tmp_path, monkeypatch):
    run = Snapshots(
        ([node(1, "h1", 1), node(2, "old2", 2), node(3, "old3", 3)], LAST),
        ([node(1, "h1", 1), node(9, "h9", 2, jump=True), node(2, "new2", 3), node(4, "h4", 4)], LAST),
    )
    book = journal(tmp_path, run, monkeypatch)
    before = book.groups()
    record = book.write("priority", URL, "head9", before, book.groups())
    assert written(tmp_path) == [record]
    assert record["at"] == "2026-10-16T01:16:43+00:00"
    assert record["action"] == "priority"
    assert record["token_actor"] == "tcc-main-ci[bot]"
    assert record["agent"] == "master@d44a5a-1"
    assert record["pull"] == URL
    assert record["expected_head"] == "head9"
    assert record["rebuilt"] == [
        {"pull": URL[:-1] + "2", "old_head": "old2", "new_head": "new2", "old_base": "b0", "new_base": "b0"}
    ]
    assert record["observed_between"] == ["2026-10-16T01:16:43+00:00", "2026-10-16T01:16:43+00:00"]
    assert record["removed"] == [URL[:-1] + "3"]
    assert record["added"] == [URL, URL[:-1] + "4"]
    assert [g["head"] for g in record["old_groups"]] == ["h1", "old2", "old3"]
    assert record["gaps"] == []
    assert len(record["id"]) == 32


def test_an_unreadable_queue_records_the_gap_instead_of_an_inferred_rebuild(tmp_path, monkeypatch):
    book = journal(tmp_path, Snapshots(fail="HTTP 502"), monkeypatch)
    record = book.write("dequeue", URL, "head9", book.groups(), book.groups(), ["refused"])
    assert record["old_groups"] is None and record["new_groups"] is None
    assert "rebuilt" not in record
    assert record["gaps"] == ["queue unreadable: HTTP 502", "queue unreadable: HTTP 502"]
    assert record["errors"] == ["refused"]
    assert record["token_actor"] is None


def test_one_unreadable_snapshot_records_no_rebuild(tmp_path, monkeypatch):
    book = journal(tmp_path, Snapshots(), monkeypatch)
    record = book.write("queue", URL, "head9", None, [queue_record.group(node(1, "h1", 1))])
    assert "rebuilt" not in record


def test_the_journal_defaults_to_the_agentihooks_home_under_the_user_home(tmp_path, monkeypatch):
    book = journal(tmp_path, Snapshots(), monkeypatch)
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    monkeypatch.setattr(queue_record.Path, "home", lambda: tmp_path)
    record = book.write("queue", URL, "head9", None, None)
    lines = (tmp_path / ".agentihooks" / "merge-queue-events.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [record]


def test_the_journal_creates_a_nested_home(tmp_path, monkeypatch):
    book = journal(tmp_path / "a" / "b", Snapshots(), monkeypatch)
    book.write("queue", URL, "head9", None, None)
    assert (tmp_path / "a" / "b" / "merge-queue-events.jsonl").exists()


def test_observed_between_spans_the_first_and_last_queue_read(tmp_path, monkeypatch):
    ticks = iter(datetime(2026, 10, 16, 1, 0, second, tzinfo=UTC) for second in range(10))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    run = Snapshots(*[([node(1, "h1", 1)], LAST)] * 3)
    book = queue_record.Journal(URL, run=run, clock=lambda: next(ticks))
    for _ in range(3):
        book.groups()
    record = book.write("queue", URL, "head9", None, None)
    assert record["observed_between"] == ["2026-10-16T01:00:00+00:00", "2026-10-16T01:00:02+00:00"]


class Book:
    def __init__(self):
        self.reads = 0
        self.records = []

    def groups(self):
        self.reads += 1
        return [{"pull": "read", "head": str(self.reads)}]

    def write(self, action, pull, head, before, after, errors=(), **extra):
        self.records.append((action, pull, head, before[0]["head"], after[0]["head"], list(errors), extra))


def test_operate_journals_each_enqueue_with_the_queue_before_and_after():
    queue = FakeQueue()
    book = Book()
    merge_queue.operate("queue", URL, run=queue.run, journal=book)
    assert book.records == [("queue", URL, "head9", "1", "2", [], {})]


def test_operate_journals_a_dequeue_and_a_refused_mutation():
    queue = FakeQueue(queued=True)
    book = Book()
    merge_queue.operate("dequeue", URL, run=queue.run, journal=book)
    assert book.records == [("dequeue", URL, "head9", "1", "2", [], {})]

    def refuse(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "enqueue refused")
        return queue.run(command, **kwargs)

    with pytest.raises(SwarmError):
        merge_queue.operate("queue", URL, run=refuse, journal=book)
    assert book.records[-1] == ("queue", URL, "head9", "3", "4", ["enqueue refused"], {})


def test_state_reads_record_nothing():
    book = Book()
    merge_queue.operate("state", URL, run=FakeQueue().run, journal=book)
    assert book.records == []


def test_priority_journals_one_record_with_every_mutation_step():
    queue = FakeQueue()
    book = Book()
    merge_queue.prioritize(URL, run=queue.run, journal=book)
    other = URL[:-1]
    assert book.records == [
        (
            "priority",
            URL,
            "head9",
            "1",
            "2",
            [],
            {
                "steps": [
                    ["dequeue", other + "3"],
                    ["dequeue", other + "2"],
                    ["queue", URL],
                    ["restore", other + "2"],
                    ["restore", other + "3"],
                ]
            },
        )
    ]


def test_priority_journals_its_steps_when_an_unexpected_error_escapes():
    queue = FakeQueue()
    book = Book()

    def run(command, **kwargs):
        if "dequeuePullRequest" in " ".join(command):
            raise RuntimeError("runner lost")
        return queue.run(command, **kwargs)

    with pytest.raises(RuntimeError):
        merge_queue.prioritize(URL, run=run, journal=book)
    assert book.records == [("priority", URL, "head9", "1", "2", [], {"steps": [["dequeue", URL[:-1] + "3"]]})]


def test_priority_journals_the_errors_it_returns():
    queue = FakeQueue()
    book = Book()

    def run(command, **kwargs):
        if "enqueuePullRequest" in " ".join(command) and "id=P9" in command:
            return subprocess.CompletedProcess(command, 1, "", "insertion refused")
        return queue.run(command, **kwargs)

    result = merge_queue.prioritize(URL, run=run, journal=book)
    assert book.records[0][5] == result["errors"] == ["insertion refused"]
