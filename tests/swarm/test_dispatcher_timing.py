import json

import pytest

from scripts.swarm import bottleneck, capacity, dispatcher, release_wait, timing
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm import test_dispatcher

pytestmark = pytest.mark.unit
store, home, shipped = test_dispatcher.store, test_dispatcher.home, test_dispatcher.shipped
SLUG, NOW, Ledger, doc, task = (
    test_dispatcher.SLUG,
    test_dispatcher.NOW,
    test_dispatcher.Ledger,
    test_dispatcher.doc,
    test_dispatcher.task,
)


def finished(capsys):
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return [row["step"] for row in rows if row.get("event") == "swarm_tick_step" and row["phase"] == "finished"]


def released_doc():
    return doc([*test_dispatcher.deep_and_wide(), task("r", phase="", after_release="v1.0.0")], phases=[])


@pytest.fixture
def tagged(monkeypatch):
    monkeypatch.setattr(release_wait, "lookup", lambda repo: (["v1.0.1"], ""))


def test_a_queue_rank_pass_times_each_part(store, home, shipped, tagged, capsys):
    store.update(SLUG, autonomy="delegate", max_eng=2)
    with timing.tick(SLUG):
        dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), released_doc(), NOW)
    steps = finished(capsys)
    parts = [
        timing.named(bottleneck.read),
        timing.named(hierarchy.project),
        timing.named(release_wait.tags),
        timing.named(dispatcher._claimable),
        timing.named(dispatcher.leverage),
        timing.named(dispatcher._saved_heads),
        timing.named(dispatcher.queue),
        timing.named(dispatcher._heads),
        timing.named(capacity.read),
        timing.named(dispatcher._head_steps),
        timing.named(Ledger.comment),
        timing.named(dispatcher.log),
    ]
    assert [part for part in parts if part not in steps] == []


def test_a_proposing_rank_pass_times_its_order_and_proposals(store, home, shipped, tagged, capsys):
    store.update(SLUG, autonomy="assist", max_eng=2)
    with timing.tick(SLUG):
        dispatcher.rank_pass(SLUG, store.config(SLUG), store, Ledger(), released_doc(), NOW)
    steps = finished(capsys)
    parts = [timing.named(dispatcher._claimable), timing.named(dispatcher.ordered), timing.named(dispatcher._propose)]
    assert [part for part in parts if part not in steps] == []
