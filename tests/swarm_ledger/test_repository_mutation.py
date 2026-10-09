from types import SimpleNamespace

import ledger_alerts
import ledger_artifacts
import ledger_media
import ledger_notifications
import ledger_plans
import ledger_priorities
import pytest

from scripts.swarm_ledger.repository import mutation


class Context:
    def __init__(self, meta, at):
        self.at, self.rev, self.events, self.dirty, self.refused = at, meta["rev"] + 1, [], False, []


def domain(calls, chat_kept=2, events_kept=2):
    def apply_changes(doc, changes, ctx):
        calls.append(("changes", doc, changes))
        ctx.dirty = "dirty" in changes
        ctx.refused.extend(change for change in changes if change.startswith("refuse"))
        return [change for change in changes if change.startswith("bad")]

    def gated(gate, doc, op, ctx):
        calls.append(("op", gate, doc, op["id"]))
        if op["id"].startswith("refused"):
            return False
        ctx.events.append(op["id"])
        return True

    return SimpleNamespace(
        Context=Context,
        now_ms=lambda: 50,
        earliest=lambda meta, at: calls.append(("earliest", dict(meta), at)) or 7,
        apply_changes=apply_changes,
        gated=gated,
        warnings=lambda doc: list(doc.get("big", [])),
        CHAT_KEPT=chat_kept,
        EVENTS_KEPT=events_kept,
    )


@pytest.fixture
def derived(monkeypatch):
    calls = []
    monkeypatch.setattr(ledger_artifacts, "sweep", lambda slug, doc, ctx: calls.append(("sweep", slug, doc, ctx.at)))
    monkeypatch.setattr(
        ledger_media, "attach_paths", lambda slug, doc, events: calls.append(("media", slug, doc, events))
    )
    monkeypatch.setattr(ledger_priorities, "derive", lambda doc, ctx: calls.append(("priorities", doc, ctx.at)))
    monkeypatch.setattr(ledger_notifications, "derive", lambda doc, ctx: calls.append(("notifications", doc, ctx.at)))
    monkeypatch.setattr(
        ledger_alerts, "derive", lambda doc, ctx, found, kept: calls.append(("alerts", doc, ctx, list(found), kept))
    )
    return calls


def test_apply_folds_changes_and_ops_in_order_and_records_the_change(derived):
    calls = []
    doc = {"chat": [1, 2, 3, 4, 5], "big": ["w1"]}
    meta = {"rev": 4, "events": ["e0"], "warnings": ["old"]}
    ops = [{"op": "stats_sync", "id": "s"}, {"op": "add", "id": "refused"}, {"op": "add", "id": "a"}]
    doc["_meta"] = meta
    rejected, ctx = mutation.apply("demo", doc, domain(calls), ["bad-1", "refuse-1"], ops, "G")
    assert rejected == ["bad-1", "refused"]
    assert calls == [
        ("earliest", {"rev": 4, "events": ["e0"], "warnings": ["old"], "members": {}}, 50),
        ("changes", doc, ["bad-1", "refuse-1"]),
        ("op", "G", doc, "refused"),
        ("op", "G", doc, "a"),
        ("op", "G", doc, "s"),
    ]
    assert derived == [
        ("sweep", "demo", doc, 50),
        ("media", "demo", doc, ["a", "s"]),
        ("priorities", doc, 50),
        ("notifications", doc, 50),
        ("alerts", doc, ctx, [(ledger_alerts.SIZE, "w1"), (ledger_alerts.SYNC, "refuse-1")], ["old"]),
    ]
    assert doc["chat"] == [4, 5]
    assert doc["_meta"] is meta
    assert ctx.changed is True
    assert meta == {
        "rev": 5,
        "events": ["a", "s"],
        "warnings": ["w1", "refuse-1"],
        "members": {},
        "created_at": 7,
        "updated_at": 50,
    }


def test_apply_hands_the_ops_and_every_rejected_id_to_the_plan_drop(derived, monkeypatch):
    seen = []
    monkeypatch.setattr(ledger_plans, "drop_refused", lambda *args: seen.append(args))
    doc = {"chat": [], "_meta": {"rev": 1, "events": [], "warnings": []}}
    ops = [{"op": "stats_sync", "id": "s"}, {"op": "add", "id": "refused"}]
    rejected, ctx = mutation.apply("demo", doc, domain([]), ["bad-1"], ops, None)
    assert seen == [(doc, [ops[1], ops[0]], ["bad-1", "refused"], ctx)]


def test_apply_without_anything_to_change_leaves_meta_alone(derived):
    meta = {"rev": 4, "events": ["e0"], "warnings": [], "members": {"m": {"role": "member"}}}
    doc = {"chat": []}
    doc["_meta"] = meta
    rejected, ctx = mutation.apply("demo", doc, domain([]))
    assert (rejected, ctx.changed) == ([], False)
    assert meta == {"rev": 4, "events": ["e0"], "warnings": [], "members": {"m": {"role": "member"}}, "created_at": 7}
    assert derived[-1] == ("alerts", doc, ctx, [], [])


@pytest.mark.parametrize(
    ("changes", "ops", "doc", "created"),
    [
        (["dirty"], None, {"chat": []}, False),
        (None, [{"op": "add", "id": "a"}], {"chat": []}, False),
        (None, None, {"chat": [], "big": ["w"]}, False),
        (None, None, {"chat": []}, True),
    ],
)
def test_each_kind_of_change_alone_moves_the_revision(derived, changes, ops, doc, created):
    meta = {"rev": 4, "events": [], "warnings": []}
    doc["_meta"] = meta
    _, ctx = mutation.apply("demo", doc, domain([]), changes, ops, created=created)
    assert (ctx.changed, meta["rev"], meta["updated_at"]) == (True, 5, 50)
