import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import doc_report
from tests.doctor.test_doctor_cli import FileLedger, core, new_ledger, state
from tests.swarm.test_cli import env, run  # noqa: F401

SLUG = "doc-report"
PR = "https://github.com/o/r/pull/7"
AT = 1_792_850_000_000
WATCHDOG = "master-watchdog"


@pytest.fixture
def world(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.delenv(doc_report.ADDRESS_ENV, raising=False)
    monkeypatch.delenv(doc_report.ACK_MINUTES_ENV, raising=False)
    assert new_ledger.create(
        SLUG, {"title": "Doc", "overview": "o", "phases": [{"title": "Build", "description": "d"}]}
    )
    ledger = FileLedger()
    ledger.add_task(SLUG, {"task": "t1", "title": "Ship the report", "lane": "eng"}, "init-swarm")
    ledger.add_task(SLUG, {"task": "t2", "title": "Unnamed work", "lane": "eng"}, "init-swarm")
    ledger.update_task(
        SLUG,
        "t1",
        {
            "state": "done",
            "pr_url": PR,
            "doc_items": ["10 Report every completed item"],
            "proof": {"evidence": "run 42"},
        },
    )
    ledger.update_task(SLUG, "t2", {"state": "done", "pr_url": PR})
    return InboxStore(fakeredis.FakeRedis(decode_responses=True)), ledger


def task(tid):
    return next(t for t in state(SLUG)["tasks"] if t["id"] == tid)


def sweep(world, at=AT):
    inbox, ledger = world
    return doc_report.report_pass(inbox, ledger, SLUG, state(SLUG)["tasks"], at)


def test_a_done_task_naming_a_doc_item_reports_it_to_the_watchdog_once(world):
    inbox, _ = world
    assert sweep(world) == [f"reported doc items of t1 to {WATCHDOG}"]
    [item] = inbox.inbox(WATCHDOG)
    assert item.task == "t1" and item.id == doc_report.report_id(SLUG, "t1", PR)
    for part in ("Status: done", "10 Report every completed item", PR, "evidence: run 42", doc_report.utc(AT)):
        assert part in item.text
    assert task("t1")["watchdog"] == {"item": item.id, "to": WATCHDOG, "pr": PR, "sent_at": doc_report.utc(AT)}
    assert sweep(world) == []
    assert len(inbox.inbox(WATCHDOG)) == 1
    assert "watchdog" not in task("t2")


def test_the_watchdog_reply_is_recorded_on_the_task(world):
    inbox, _ = world
    sweep(world)
    inbox.reply(doc_report.report_id(SLUG, "t1", PR), WATCHDOG, "Closed doc item 10")
    assert sweep(world, AT + 60_000) == ["recorded the watchdog acknowledgment of t1"]
    record = task("t1")["watchdog"]
    assert record["ack"] == "Closed doc item 10" and record["acked_at"] == doc_report.utc(AT + 60_000)
    assert sweep(world, AT + 120_000) == []


def test_a_watchdog_close_without_a_reply_records_its_detail(world):
    inbox, _ = world
    sweep(world)
    inbox.close(doc_report.report_id(SLUG, "t1", PR), WATCHDOG, "done", "item 10 closed on the doc")
    assert sweep(world, AT + 60_000) == ["recorded the watchdog acknowledgment of t1"]
    assert task("t1")["watchdog"]["ack"] == "item 10 closed on the doc"


def test_a_cancelled_report_counts_as_answered_and_raises_no_priority(world):
    inbox, _ = world
    sweep(world)
    inbox.close(doc_report.report_id(SLUG, "t1", PR), WATCHDOG, "cancel", "not a doc item")
    late = AT + doc_report.ack_ms()
    assert sweep(world, late) == ["recorded the watchdog acknowledgment of t1"]
    assert task("t1")["watchdog"]["ack"] == "not a doc item"
    assert state(SLUG)["priorities"] == []


def test_a_report_closed_as_blocked_is_still_unacknowledged(world):
    inbox, _ = world
    sweep(world)
    inbox.close(doc_report.report_id(SLUG, "t1", PR), WATCHDOG, "blocked", "the doc is unreachable")
    late = AT + doc_report.ack_ms()
    assert sweep(world, late) == ["raised a Priority for the unacknowledged doc item report of t1"]
    assert "ack" not in task("t1")["watchdog"]


def test_a_completion_with_a_new_pull_request_sends_a_new_report(world):
    inbox, ledger = world
    sweep(world)
    later = "https://github.com/o/r/pull/8"
    ledger.update_task(SLUG, "t1", {"pr_url": later})
    assert sweep(world, AT + 60_000) == [f"reported doc items of t1 to {WATCHDOG}"]
    assert [i.id for i in inbox.inbox(WATCHDOG)] == [
        doc_report.report_id(SLUG, "t1", PR),
        doc_report.report_id(SLUG, "t1", later),
    ]
    assert task("t1")["watchdog"]["pr"] == later


def test_a_recorded_report_missing_from_the_inbox_is_sent_again(world):
    inbox, ledger = world
    record = {"item": doc_report.report_id(SLUG, "t1", PR), "to": WATCHDOG, "pr": PR, "sent_at": doc_report.utc(AT)}
    ledger.update_task(SLUG, "t1", {"watchdog": record})
    assert sweep(world, AT + 60_000) == [f"resent the doc item report of t1 to {WATCHDOG}"]
    [item] = inbox.inbox(WATCHDOG)
    assert item.id == record["item"] and item.text == doc_report.text(SLUG, task("t1"), AT)
    assert sweep(world, AT + 120_000) == []


def test_an_unacknowledged_report_raises_one_priority_after_the_window(world):
    sweep(world)
    assert sweep(world, AT + doc_report.ack_ms() - 1) == []
    late = AT + doc_report.ack_ms()
    assert sweep(world, late) == ["raised a Priority for the unacknowledged doc item report of t1"]
    assert [p["text"] for p in state(SLUG)["priorities"] if p["item"] == "tasks/t1"] == [doc_report.ASK]
    assert task("t1")["watchdog"]["priority"] == doc_report.utc(late)
    assert sweep(world, late + 60_000) == []


def test_the_watchdog_address_and_window_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(doc_report.ADDRESS_ENV, "watch@elsewhere")
    monkeypatch.setenv(doc_report.ACK_MINUTES_ENV, "5")
    assert doc_report.address() == "watch@elsewhere" and doc_report.ack_ms() == 300_000


def test_the_default_window_is_thirty_minutes_in_utc(monkeypatch):
    import time

    monkeypatch.delenv(doc_report.ACK_MINUTES_ENV, raising=False)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        assert doc_report.ack_ms() == 1_800_000
        assert doc_report.utc(0) == "1970-01-01T00:00:00Z"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_report_text_names_every_field():
    task = {
        "id": "t9",
        "title": "Ship",
        "doc_items": ["10 a", "13 b"],
        "pr_url": PR,
        "proof": {"evidence": "r", "fix": "f"},
    }
    assert doc_report.text("sw", task, 0) == "\n".join(
        [
            "Doc item report from swarm sw, task t9: Ship",
            "Status: done",
            "Doc items: 10 a; 13 b",
            f"Merged pull request: {PR}",
            "Proof run: evidence: r; fix: f",
            "Done at: 1970-01-01T00:00:00Z",
            "Reply to this message to acknowledge it; the ledger records your reply on the task.",
        ]
    )
    bare = doc_report.text("sw", {"id": "t9", "doc_items": ["10 a"]}, 0).splitlines()
    assert bare[0] == "Doc item report from swarm sw, task t9: "
    assert bare[3:5] == ["Merged pull request: none recorded", "Proof run: none recorded"]


def test_a_done_task_without_a_pull_request_is_reported_once(world):
    _, ledger = world
    ledger.add_task(SLUG, {"task": "t3", "title": "No pull request", "lane": "eng"}, "init-swarm")
    ledger.update_task(SLUG, "t3", {"state": "done", "doc_items": ["18 Watchdog feedback stays internal"]})
    assert sweep(world) == [f"reported doc items of t1 to {WATCHDOG}", f"reported doc items of t3 to {WATCHDOG}"]
    assert task("t3")["watchdog"]["pr"] == ""
    assert sweep(world) == []


def test_task_set_parses_doc_items_as_a_list():
    import ledger

    assert ledger._task_set_fields(["doc_items=10 a,13 b"]) == {"doc_items": ["10 a", "13 b"]}


def test_swarm_done_sends_the_report_as_the_engineer(env, monkeypatch, capsys):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    ledger.rows["t1"]["doc_items"] = ["13 Every completion triggers a watchdog update"]
    authors = []
    report = doc_report.report
    monkeypatch.setattr(doc_report, "report", lambda *a, by: authors.append(by) or report(*a, by=by))
    capsys.readouterr()
    assert run("sw", "done", "--pr", PR) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["state"] == "done"
    [item] = InboxStore(store.redis).inbox(WATCHDOG)
    assert "13 Every completion triggers a watchdog update" in item.text and PR in item.text
    assert ledger.rows["t1"]["watchdog"]["item"] == item.id
    assert authors == ["engineer@a1b2c3-0001"]
