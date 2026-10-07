import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_alerts  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_server  # noqa: E402
import new_ledger  # noqa: E402

from scripts.inbox.seats import seat_address  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "alerts-2026-01-01"
LONG = " ".join(["word"] * 120)
SIZE_TEXT = "phase {} description has 120 words, limit 100"


def make_ledger(description="d"):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": description}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    return state


def refuse_a_plan():
    taken = {"op": "phase_append", "id": "plan", "by": "master", "phases": [{"phase": "p1", "title": "Taken"}]}
    return core.sync(SLUG, ops=[taken])[0]


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
    state, _ = core.sync(SLUG)
    [alert] = state["alerts"]
    assert alert["text"] == SIZE_TEXT.format(phase)
    assert (alert["source"], alert["target"], alert["state"]) == ("size", "master", "open")
    assert alert["id"] and isinstance(alert["at"], int) and alert["at"] > 0


def test_a_ledger_without_warnings_raises_no_alert():
    assert make_ledger()["alerts"] == []


def test_a_sync_refusal_becomes_an_alert_for_the_operator():
    make_ledger()
    state = refuse_a_plan()
    [alert] = state["alerts"]
    assert (alert["source"], alert["target"], alert["state"]) == ("sync", "operator", "open")
    assert alert["text"] in state["_meta"]["warnings"]


def test_a_closed_alert_stays_closed_while_its_warning_lasts():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    core.sync(SLUG, ops=[{"op": "alert_close", "id": "x1", "target": alert, "outcome": "Phase trimmed later"}])
    state, _ = core.sync(SLUG)
    assert [a["state"] for a in state["alerts"]] == ["done"]


def test_claim_records_the_claimant_and_close_records_the_outcome():
    state = make_ledger(LONG)
    alert = state["alerts"][0]["id"]
    state, rejected = core.sync(SLUG, ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
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
    core.sync(SLUG, ops=[{"op": "alert_claim", "id": "k1", "target": alert, "by": "boss"}])
    state, rejected = core.sync(SLUG, ops=[{"op": "alert_claim", "id": "k2", "target": alert}])
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


def test_an_operator_alert_reaches_the_operator_inbox(inbox):
    make_ledger()
    state = refuse_a_plan()
    ledger_server.deliver_alerts(SLUG, state)
    [item] = inbox.inbox("operator")
    assert state["alerts"][0]["text"] in item.text


def test_the_page_header_renders_no_ledger_warnings():
    page = (SCRIPTS / "template.html").read_text(encoding="utf-8")
    assert "meta.warnings" not in page


def test_the_cli_claims_and_closes_an_alert_as_its_caller(monkeypatch):
    import ledger

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
