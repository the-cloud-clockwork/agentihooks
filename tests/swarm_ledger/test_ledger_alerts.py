import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_alerts  # noqa: E402
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.inbox.seats import seat_address  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig  # noqa: E402
from scripts.swarm_ledger import ledger, ledger_server  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import page_source  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "alerts-2026-01-01"
LONG = " ".join(["word"] * 120)
SIZE_TEXT = "phase {} description has 120 words, limit 100"


def sync(ops=None):
    return core.sync(SLUG, ops=ops)


def make_ledger(description="d"):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": description}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = sync()
    return state


def refuse_a_plan():
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    return sync(ops=[taken])[0]


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    box = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: box)
    return box


def test_a_size_warning_becomes_one_open_alert_for_the_master():
    state = make_ledger(LONG)
    phase = state["phases"][0]["id"]
    state, _ = sync()
    [alert] = state["alerts"]
    assert alert["text"] == SIZE_TEXT.format(phase)
    assert (alert["source"], alert["target"], alert["state"]) == ("size", "master", "open")
    assert alert["id"] == f"al-{alert['rev']}-0" and alert["rev"] == 1
    assert isinstance(alert["at"], int) and alert["at"] > 0


def test_a_ledger_without_warnings_raises_no_alert():
    assert make_ledger()["alerts"] == []


def test_a_sync_refusal_becomes_an_alert_for_the_writer():
    make_ledger()
    state = refuse_a_plan()
    [alert] = state["alerts"]
    assert (alert["source"], alert["target"], alert["state"]) == ("sync", "master", "open")
    assert alert["text"] in state["_meta"]["warnings"]


def test_a_closed_alert_stays_closed_while_its_warning_lasts():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    sync(ops=[{"op": "alert_close", "id": "x1", "target": alert, "outcome": "Phase trimmed later"}])
    state, _ = sync()
    assert [a["state"] for a in state["alerts"]] == ["done"]


def test_claim_records_the_claimant_and_close_records_the_outcome():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    state, rejected = sync(ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
    assert rejected == []
    assert (state["alerts"][0]["state"], state["alerts"][0]["claimed_by"]) == ("claimed", "boss")
    state, rejected = core.sync(
        SLUG, ops=[{"op": "alert_close", "id": "k2", "target": alert, "outcome": "Shortened the phase"}]
    )
    assert rejected == []
    closed = state["alerts"][0]
    assert (closed["state"], closed["outcome"], closed["closed_by"]) == ("done", "Shortened the phase", "operator")
    assert closed["claimed_by"] == "boss"


def test_a_claimed_alert_cannot_be_taken_by_another_claimant():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    sync(ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
    state, rejected = sync(ops=[{"op": "alert_claim", "id": "k2", "target": alert}])
    assert rejected == ["k2"]
    assert state["alerts"][0]["claimed_by"] == "boss"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "alert_close", "id": "z", "target": "al-1-0"},
        {"op": "alert_close", "id": "z", "target": "al-1-0", "outcome": "  "},
        {"op": "alert_claim", "id": "z"},
        {"op": "alert_claim", "id": "z", "target": "al-1-0", "text": "extra"},
    ],
)
def test_malformed_alert_ops_are_refused(op):
    with pytest.raises(ValueError):
        ledger_alerts.check(op)


def test_each_new_alert_reaches_its_target_inbox_once(inbox):
    state = make_ledger(LONG)
    master = seat_address(SLUG, MASTER)
    ledger_server.deliver_alerts(SLUG, state)
    ledger_server.deliver_alerts(SLUG, state)
    [item] = inbox.inbox(master)
    assert state["alerts"][0]["id"] in item.text and state["alerts"][0]["text"] in item.text
    assert item.sender == ledger_alerts.SENDER


def test_a_refusal_without_a_live_writer_reaches_the_master_inbox(inbox):
    make_ledger()
    state = refuse_a_plan()
    ledger_server.deliver_alerts(SLUG, state)
    [item] = inbox.inbox(seat_address(SLUG, MASTER))
    assert state["alerts"][0]["text"] in item.text


def test_the_page_header_renders_no_ledger_warnings():
    page = page_source()
    assert "meta.warnings" not in page


def test_the_cli_claims_and_closes_an_alert_as_its_caller(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None, service=False: sent.extend(ops or []) or {})
    for argv in (["alert", "claim", "al-1-0"], ["alert", "close", "al-1-0", "Trimmed the phase"]):
        ledger.cmd_alert(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", *argv]))
    assert [(o["op"], o["target"], o["by"], o.get("outcome")) for o in sent] == [
        ("alert_claim", "al-1-0", "boss", None),
        ("alert_close", "al-1-0", "boss", "Trimmed the phase"),
    ]
    for op in sent:
        ledger_alerts.check(op)
    with pytest.raises(SystemExit):
        ledger.cmd_alert(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "alert", "close", "al-1-0"]))


def test_the_cli_lists_only_open_and_claimed_alerts(monkeypatch, capsys):
    rows = [{"id": "a", "state": "open"}, {"id": "b", "state": "claimed"}, {"id": "c", "state": "done"}]
    asked = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None, service=False: asked.append(slug) or {"alerts": rows})
    ledger.cmd_alert(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "alert", "list"]))
    assert asked == [SLUG]
    assert capsys.readouterr().out == json.dumps(rows[:2], indent=2) + "\n"


def test_the_cli_prints_the_alert_and_the_action_it_took(monkeypatch, capsys):
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None, service=False: {})
    for argv in (["alert", "claim", "al-1-0"], ["alert", "close", "al-1-0", "Trimmed"]):
        ledger.cmd_alert(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", *argv]))
    assert capsys.readouterr().out.splitlines() == [
        '{"alert": "al-1-0", "action": "claim"}',
        '{"alert": "al-1-0", "action": "close"}',
    ]


def test_the_cli_refuses_an_unknown_alert_action():
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "alert", "reopen", "al-1-0"])


def test_the_cli_refuses_a_claim_without_an_id_or_with_an_outcome():
    for argv in (["alert", "claim"], ["alert", "claim", "al-1-0", "why"]):
        with pytest.raises(SystemExit) as stopped:
            ledger.cmd_alert(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", *argv]))
        assert stopped.value.code == 'alert claim needs ID; alert close needs ID and "OUTCOME"'


def test_claim_and_close_are_recorded_as_events_by_their_author():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    sync(ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
    state, _ = sync(ops=[{"op": "alert_close", "id": "k2", "target": alert, "outcome": " Trimmed "}])
    events = [(e["by"], e["kind"], e["target"], e["text"]) for e in state["_meta"]["events"][-2:]]
    assert events == [
        ("boss", "alert claimed", f"alerts/{alert}", state["alerts"][0]["text"]),
        ("operator", "alert closed", f"alerts/{alert}", "Trimmed"),
    ]
    assert state["alerts"][0]["outcome"] == "Trimmed"
    assert state["alerts"][0]["claimed_at"] > 0 and state["alerts"][0]["closed_at"] > 0


def test_a_repeat_claim_by_the_holder_is_accepted_and_unknown_or_closed_alerts_refuse():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    sync(ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
    _, rejected = sync(ops=[{"op": "alert_claim", "id": "k2", "target": alert, "by": "boss"}])
    assert rejected == []
    sync(ops=[{"op": "alert_close", "id": "k3", "target": alert, "outcome": "Done"}])
    _, rejected = sync(
        ops=[
            {"op": "alert_claim", "id": "k4", "target": alert},
            {"op": "alert_close", "id": "k5", "target": "al-9-9", "outcome": "Done"},
        ]
    )
    assert rejected == ["k4", "k5"]


@pytest.mark.parametrize(
    "op, says",
    [
        ({"op": "alert_claim", "id": "z", "target": ""}, "alert_claim takes target, an alert id, and an optional by"),
        ({"op": "alert_claim", "id": "z", "target": "a", "by": " "}, "alert_claim by must be a name"),
        ({"op": "alert_claim", "id": "z", "target": "a", "by": 7}, "alert_claim by must be a name"),
        (
            {"op": "alert_close", "id": "z", "target": "a", "outcome": "x" * 2001},
            "alert_close needs an outcome of up to 2000 characters",
        ),
    ],
)
def test_alert_op_refusals_name_what_is_wrong(op, says):
    with pytest.raises(ValueError) as caught:
        ledger_alerts.check(op)
    assert str(caught.value) == says


def test_an_outcome_of_the_full_length_is_accepted():
    ledger_alerts.check({"op": "alert_close", "id": "z", "target": "a", "outcome": "x" * 2000, "by": "boss"})


def test_only_the_newest_done_alerts_are_kept():
    done = [{"id": f"d{i}", "text": f"t{i}", "state": "done"} for i in range(202)]
    doc = {"alerts": [*done, {"id": "o", "text": "open", "state": "open"}]}
    ledger_alerts.derive(doc, SimpleNamespace(rev=3, at=1, dirty=False), [], [])
    assert [a["id"] for a in doc["alerts"]] == [f"d{i}" for i in range(2, 202)] + ["o"]


def derived(rows, raised, before):
    doc, ctx = {"alerts": rows}, SimpleNamespace(rev=7, at=1, dirty=False)
    ledger_alerts.derive(doc, ctx, raised, before)
    return [(a["text"], a["state"]) for a in doc["alerts"]], ctx.dirty


def test_an_open_text_is_not_raised_twice_and_a_closed_one_returns_after_its_warning_cleared():
    rows = [{"id": "o", "text": "x", "state": "open"}, {"id": "d", "text": "y", "state": "done"}]
    found, dirty = derived(rows, [("sync", "x"), ("sync", "y")], [])
    assert found == [("x", "open"), ("y", "done"), ("y", "open")] and dirty is True


def test_warnings_already_known_before_the_first_alert_still_become_alerts():
    assert derived([], [("size", "x")], ["x"]) == ([("x", "open")], True)


def test_one_warning_found_twice_in_a_sync_raises_one_alert():
    assert derived([], [("sync", "x"), ("sync", "x")], []) == ([("x", "open")], True)


def test_a_skipped_warning_does_not_stop_the_ones_after_it():
    rows = [{"id": "o", "text": "x", "state": "open"}]
    assert derived(rows, [("sync", "x"), ("sync", "z")], [])[0] == [("x", "open"), ("z", "open")]


def test_nothing_new_leaves_the_sync_clean():
    assert derived([{"id": "o", "text": "x", "state": "open"}], [("sync", "x")], ["x"])[1] is False


def test_an_alert_op_on_a_ledger_without_alerts_is_refused():
    op = {"op": "alert_claim", "id": "k", "target": "al-1-0"}
    assert ledger_alerts.apply({}, op, SimpleNamespace(at=1)) is False


def test_the_alert_message_names_the_alert_and_how_to_claim_and_close_it():
    alert = {"id": "al-4-0", "source": "size", "text": "phase p1 description has 120 words, limit 100"}
    assert ledger_alerts.message("demo", alert) == (
        "On ledger demo: alert al-4-0 (size): phase p1 description has 120 words, limit 100. Claim it with "
        "agentihooks ledger --slug demo --as <you> alert claim al-4-0, then close it with alert close "
        'al-4-0 "<outcome>".'
    )


def test_delivery_marks_each_alert_sent_for_thirty_days_and_skips_claimed_and_sent_ones(inbox):
    claimed = {"id": "al-5-0", "text": "t", "source": "size", "target": "master", "state": "claimed", "rev": 5}
    sent_before = {**claimed, "id": "al-5-1", "state": "open"}
    fresh = {**claimed, "id": "al-5-2", "state": "open"}
    ledger_alerts.deliver(inbox, SLUG, [sent_before], 5, "master@x")
    sent = ledger_alerts.deliver(inbox, SLUG, [claimed, sent_before, fresh], 5, "master@x")
    assert [(item.address, item.text) for item in sent] == [("master@x", ledger_alerts.message(SLUG, fresh))]
    key = inbox.key("alert-sent", SLUG, "al-5-2")
    assert inbox.redis.ttl(key) == 30 * 24 * 3600 and inbox.redis.get(key) == "1"


def test_alerts_from_an_earlier_sync_are_not_sent_again(inbox, capsys):
    make_ledger(LONG)
    state, _ = sync(ops=[{"op": "add", "thread": "notes", "id": "n1", "text": "Later note"}])
    assert ledger_server.deliver_alerts(SLUG, state) == []
    assert inbox.inbox(seat_address(SLUG, MASTER)) == []
    assert capsys.readouterr().err == ""


def test_a_finished_master_is_not_the_alert_address(inbox):
    RedisStore(inbox.redis).put_agent(
        SLUG, AgentRecord(name="master@old", lane=MASTER, task="", state="finished", seat="old-seat")
    )
    state = make_ledger(LONG)
    ledger_server.deliver_alerts(SLUG, state)
    assert len(inbox.inbox(seat_address(SLUG, MASTER))) == 1
    assert inbox.inbox("old-seat") == []


def test_an_unreachable_inbox_leaves_the_write_standing(monkeypatch, capsys):
    def refuse(environ=None):
        raise RuntimeError("no redis")

    monkeypatch.setattr("scripts.inbox.store.connect", refuse)
    state = make_ledger(LONG)
    assert ledger_server.deliver_alerts(SLUG, state) == []
    assert capsys.readouterr().err == f"alert delivery for {SLUG}: no redis\n"


def test_the_server_reply_delivers_new_alerts(inbox):
    make_ledger()
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.path = f"/api/{SLUG}?view=agent"
    handler.send = lambda code, body, ctype: None
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    handler.reply_state(SLUG, ops=[taken])
    [item] = inbox.inbox(seat_address(SLUG, MASTER))
    assert "phase id p1 is already taken" in item.text


def test_versioned_write_delivers_alert_once(inbox):
    from scripts.swarm_ledger.api import mutations, resources

    state = make_ledger()
    op = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    payload = {
        "operation_id": "refused-plan",
        "ops": [op],
        "guards": {"phases": resources.resource_revision(state, "phases")},
    }
    mutations.apply(ledger_server, SLUG, "", payload)
    mutations.apply(ledger_server, SLUG, "", payload)
    items = inbox.inbox("master") + inbox.inbox(seat_address(SLUG, MASTER)) + inbox.inbox("operator")
    assert len(items) == 1
    assert "phase id p1 is already taken" in items[0].text


@pytest.mark.parametrize("seat", ["eng-1", ""])
def test_refusal_reaches_writer_seat_or_agent(inbox, seat):
    make_ledger()
    writer = "writer"
    store = RedisStore(inbox.redis)
    store.put_agent(SLUG, AgentRecord(name=writer, lane="eng", task="", seat=seat, state="working"))
    state, rejected = sync(
        [{"op": "phase_append", "id": "refusal", "by": writer, "phases": [{"phase": "p1", "title": "Taken"}]}]
    )
    assert rejected == ["refusal"]
    ledger_server.deliver_alerts(SLUG, state)
    address = seat_address(SLUG, seat) if seat else writer
    assert len(inbox.inbox(address)) == 1
    assert inbox.inbox("operator") == []
    assert state["alerts"][0]["writer"] == writer


def test_successful_retry_closes_only_same_writer_and_item():
    make_ledger()
    refused = {"op": "phase_append", "id": "refusal", "by": "writer", "phases": [{"phase": "p1", "title": "Taken"}]}
    state, _ = sync([refused])
    alert_id = state["alerts"][0]["id"]
    sync([{"op": "phase_append", "id": "other", "by": "other", "phases": [{"phase": "p2", "title": "Second"}]}])
    state, _ = sync(
        [{"op": "phase_append", "id": "retry", "by": "writer", "phases": [{"phase": "p3", "title": "Third"}]}]
    )
    alert = next(a for a in state["alerts"] if a["id"] == alert_id)
    assert alert["state"] == "done"
    assert alert["outcome"] == "The writer succeeded on the same item."
    assert alert["closed_by"] == "writer"
    assert any(e["kind"] == "alert closed" for e in state["_meta"]["events"])


def test_refusal_expires_one_hour_after_last_repeat(monkeypatch):
    make_ledger()
    clock = [10000000]
    monkeypatch.setattr(core, "now_ms", lambda: clock[0])
    op = {"op": "phase_append", "id": "refusal", "by": "writer", "phases": [{"phase": "p1", "title": "Taken"}]}
    state, _ = sync([op])
    alert_id = state["alerts"][0]["id"]
    clock[0] += 3599999
    state, _ = sync([op])
    assert len(state["alerts"]) == 1
    clock[0] += 3599999
    state, _ = sync()
    assert state["alerts"][0]["state"] == "open"
    clock[0] += 1
    state, _ = sync()
    alert = next(a for a in state["alerts"] if a["id"] == alert_id)
    assert alert["state"] == "done"
    assert alert["outcome"] == "No repeat refusal for one hour."


def test_server_sweep_expires_refusals_without_a_new_write(monkeypatch):
    make_ledger()
    state = refuse_a_plan()
    monkeypatch.setattr(core, "now_ms", lambda: state["alerts"][0]["at"] + 3600000)
    ledger_server.expire_alerts()
    state = ledger_server.repository.get_document(SLUG)
    assert state["alerts"][0]["state"] == "done"
    assert state["alerts"][0]["outcome"] == "No repeat refusal for one hour."


def test_successful_retry_in_one_batch_closes_new_refusal():
    make_ledger()
    state, _ = sync(
        [
            {"op": "phase_append", "id": "bad", "by": "writer", "phases": [{"phase": "p1", "title": "Taken"}]},
            {"op": "phase_append", "id": "good", "by": "writer", "phases": [{"phase": "p2", "title": "New"}]},
        ]
    )
    assert state["alerts"][0]["state"] == "done"
