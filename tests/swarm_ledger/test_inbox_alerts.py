import pytest

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger import ledger_alerts
from scripts.swarm_ledger.api import schemas


class Ctx:
    def __init__(self, rev=5, at=50):
        self.rev, self.at, self.dirty, self.events = rev, at, False, []

    def record(self, by, kind, target, text=""):
        self.events.append((by, kind, target, text))


def raise_op(item, text="unread", op_id="r"):
    return {"op": "alert_raise", "id": op_id, "by": "swarm", "source": "inbox", "text": text, "item": item}


def close_op(item, op_id="c"):
    return {"op": "alert_close", "id": op_id, "by": "swarm", "item": item, "outcome": "The message was closed."}


def apply(doc, op, ctx=None):
    ledger_alerts.check(op)
    return ledger_alerts.apply(doc, op, ctx or Ctx())


def test_an_inbox_alert_is_raised_once_per_item_and_carries_the_item():
    doc = {}
    apply(doc, raise_op("m1", "unread one"))
    apply(doc, raise_op("m1", "unread one again"), Ctx(rev=6))
    apply(doc, raise_op("m2", "unread two"), Ctx(rev=7))
    assert [(a["source"], a["item"], a["text"], a["target"], a["state"]) for a in doc["alerts"]] == [
        ("inbox", "m1", "unread one", "master", "open"),
        ("inbox", "m2", "unread two", "master", "open"),
    ]


def test_two_items_with_the_same_text_each_get_their_own_alert():
    doc, ctx = {}, Ctx(rev=9, at=70)
    apply(doc, raise_op("m1", "same"), ctx)
    apply(doc, raise_op("m2", "same"), ctx)
    assert [(a["id"], a["item"], a["at"], a["rev"]) for a in doc["alerts"]] == [
        ("al-9-0", "m1", 70, 9),
        ("al-9-1", "m2", 70, 9),
    ]
    assert ctx.dirty is True


def test_closing_by_item_closes_only_that_items_open_alert():
    doc = {}
    apply(doc, raise_op("m1", "unread one"))
    apply(doc, raise_op("m2", "unread two"), Ctx(rev=6))
    ctx = Ctx(rev=8, at=90)
    assert apply(doc, close_op("m1"), ctx) is True
    assert [(a["item"], a["state"]) for a in doc["alerts"]] == [("m1", "done"), ("m2", "open")]
    closed = doc["alerts"][0]
    assert (closed["closed_by"], closed["closed_at"], closed["outcome"]) == ("swarm", 90, "The message was closed.")
    assert ctx.events == [("swarm", "alert closed", f"alerts/{closed['id']}", "The message was closed.")]


def test_a_closed_items_alert_returns_when_the_item_is_raised_again():
    doc = {}
    apply(doc, raise_op("m1", "unread one"))
    apply(doc, close_op("m1"), Ctx(rev=6))
    apply(doc, raise_op("m1", "unread one later"), Ctx(rev=7))
    assert [(a["text"], a["state"]) for a in doc["alerts"]] == [("unread one", "done"), ("unread one later", "open")]


def test_closing_by_item_leaves_other_sources_alone():
    doc = {"alerts": [{"id": "s", "source": "sync", "item": "m1", "text": "t", "state": "open"}]}
    ctx = Ctx()
    assert apply(doc, close_op("m1"), ctx) is True
    assert doc["alerts"][0]["state"] == "open" and ctx.events == []


def test_a_raise_without_an_item_still_dedupes_by_source():
    doc = {}
    apply(doc, {"op": "alert_raise", "id": "a", "by": "swarm", "source": "setup", "text": "refused"})
    apply(doc, {"op": "alert_raise", "id": "b", "by": "swarm", "source": "setup", "text": "refused again"})
    assert [(a["source"], a.get("item")) for a in doc["alerts"]] == [("setup", None)]


INBOX_REFUSED = (
    "alert_raise takes by, a source among ('promotion', 'release', 'seat', 'spawn', 'merge', 'inbox', 'setup'), "
    "a text of up to 2000 characters and, for inbox, an item"
)
CLOSE_REFUSED = "alert_close takes target, an alert id, and an optional by, or an inbox item instead of target"


@pytest.mark.parametrize(
    "op, says",
    [
        ({**raise_op("m1"), "source": "promotion"}, INBOX_REFUSED),
        (raise_op(""), INBOX_REFUSED),
        (raise_op(7), INBOX_REFUSED),
        ({k: v for k, v in raise_op("m1").items() if k != "item"}, INBOX_REFUSED),
        ({**close_op("m1"), "target": "al-1-0"}, CLOSE_REFUSED),
        ({k: v for k, v in close_op("m1").items() if k != "item"}, CLOSE_REFUSED),
        (close_op(""), CLOSE_REFUSED),
        (close_op(3), CLOSE_REFUSED),
        (
            {"op": "alert_claim", "id": "z", "item": "m1"},
            "alert_claim takes target, an alert id, and an optional by",
        ),
    ],
)
def test_malformed_inbox_alert_ops_are_refused(op, says):
    with pytest.raises(ValueError) as caught:
        ledger_alerts.check(op)
    assert str(caught.value) == says


def test_the_api_schema_accepts_an_item_on_raise_and_close():
    assert schemas.mismatched_field(schemas.operation_schema("alert_raise"), raise_op("m1")) is None
    assert schemas.mismatched_field(schemas.operation_schema("alert_close"), close_op("m1")) is None


def test_the_client_sends_the_item_on_raise_and_close(monkeypatch):
    sent = []
    client = LedgerClient()
    monkeypatch.setattr(client, "_call", lambda slug, ops: sent.append((slug, ops)))
    client.raise_alert("sw", "inbox", "unread", item="m1")
    client.raise_alert("sw", "setup", "refused")
    client.close_item_alerts("sw", "m1", "The message was closed.")
    shapes = [(slug, {k: v for k, v in op.items() if k != "id"}) for slug, (op,) in sent]
    assert shapes == [
        ("sw", {"op": "alert_raise", "by": "swarm", "source": "inbox", "text": "unread", "item": "m1"}),
        ("sw", {"op": "alert_raise", "by": "swarm", "source": "setup", "text": "refused"}),
        ("sw", {"op": "alert_close", "by": "swarm", "item": "m1", "outcome": "The message was closed."}),
    ]
