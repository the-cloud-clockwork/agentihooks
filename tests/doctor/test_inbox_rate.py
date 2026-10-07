from dataclasses import replace

import pytest

from scripts.doctor import rates, rates_read
from scripts.doctor.inbox import no_outcome
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")


def item(name, created, state="done", reason="done", fyi=False):
    return dict(
        id=name,
        created_at=created,
        state=state,
        reason=reason,
        fyi=fyi,
        sender="swarm",
        address="eng-1@test",
        text="Fix checks",
        history=[dict(state=state, by="eng-1@test")],
    )


def test_rate_counts_only_new_bare_done_work_items_and_keeps_history():
    rows = [
        item("start", 1000),
        item("middle", 2000),
        item("old", 999),
        item("end", 3601000),
        item("fyi", 2000, fyi=True),
        item("named", 2000, reason="done: checks fixed"),
        item("open", 2000, state="pending", reason=""),
        item("cancel", 2000, state="cancelled", reason="cancelled"),
    ]
    records = rates.Records([], {}, {}, {}, [], [], [], {}, inbox=rows)
    assert rates.rates(records, rates.Window(1000, 3601000))["inbox no outcome"] == {
        "items sent": 6,
        "closed done with no outcome": 2,
        "per hour": 2.0,
    }
    assert len(no_outcome(rows)) == 5
    assert rates.rates(records, rates.Window(1000, 1801000))["inbox no outcome"]["per hour"] == 4.0
    changed = replace(records, inbox=rows + [item("planted", 3000)])
    assert rates.rates(changed, rates.Window(1000, 3601000))["inbox no outcome"]["per hour"] == 3.0


def test_load_and_report_expose_live_inbox_rate(monkeypatch, tmp_path):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    inbox = InboxStore(redis)
    sent = inbox.send("swarm", "eng-1@test", "Fix checks")
    inbox.close(sent.id, "eng-1@test", "done", "recorded outcome")
    redis.hset(inbox.key("item", sent.id), "reason", "done")
    monkeypatch.setattr(rates_read.activity, "entries", lambda slug: {})
    monkeypatch.setattr(rates_read.injection_trace, "corrections", lambda: [])
    monkeypatch.setattr(rates_read, "injections", lambda at: [])

    class Ledger:
        def state(self, slug):
            return {}

    win = rates.Window(sent.created_at, sent.created_at + 3600000)
    records = rates_read.load(store, Ledger(), "test", win, home=tmp_path)
    report = rates.report(records, {"before": win})
    assert report["rates"]["before"]["inbox no outcome"]["per hour"] == 1.0
    assert "closed done with no outcome" in rates.table(report)
