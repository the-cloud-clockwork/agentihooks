from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxError
from scripts.swarm import delivery

pytestmark = pytest.mark.unit


class Inbox:
    def __init__(self, items):
        self.items = items

    def get(self, item_id):
        if item_id not in self.items:
            raise InboxError(f"no message {item_id}")
        return self.items[item_id]


def item(ref, sender="master@abc-1"):
    return SimpleNamespace(id="answer-1", ref=ref, sender=sender, text="the deploy is green")


ASKED = {
    "asked-1": SimpleNamespace(sender="operator", ref="sw:41:m-7f3a"),
    "agent-1": SimpleNamespace(sender="engineer@abc-2", ref=""),
}


def test_an_answer_to_an_operator_chat_line_carries_its_id():
    assert delivery.operator_line(Inbox(ASKED), "sw", item("asked-1")) == "m-7f3a"


@pytest.mark.parametrize(
    ("ref", "slug"),
    [("", "sw"), ("missing", "sw"), ("agent-1", "sw"), ("asked-1", "other")],
)
def test_an_item_that_answers_no_operator_line_on_this_ledger_carries_none(ref, slug):
    assert delivery.operator_line(Inbox(ASKED), slug, item(ref)) == ""


def test_show_relays_the_answer_with_the_operator_line_it_replies_to():
    calls = []
    ledger = SimpleNamespace(relay=lambda *args: calls.append(args))

    delivery.show(ledger, "sw", item("asked-1"), "master@abc-1", "", "m-7f3a")

    assert calls == [("sw", "the deploy is green", "master@abc-1", "answer-1", "m-7f3a")]
