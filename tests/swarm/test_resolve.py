import json
from types import SimpleNamespace

import pytest

from scripts.swarm import resolve
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_blockers

pytestmark = pytest.mark.unit

ITEM = "tasks/t1"


class Ledger:
    def __init__(self):
        self.doc = {"tasks": [{"id": "t1", "title": "Deploy the hive", "comments": []}], "priorities": []}
        self.refused = []

    def item(self, slug, path):
        return ledger_blockers.find(self.doc, path)

    def blocker(self, slug, kind, by, item, fields):
        op = {"op": kind, "id": f"{kind}-{len(self.refused)}", "by": by, "item": item, **fields}
        ledger_blockers.check(op)
        ctx = SimpleNamespace(slug=slug, at=1, refused=[], record=lambda *a, **k: None, stamp=lambda *a: None)
        if not ledger_blockers.apply(self.doc, op, ctx):
            raise SwarmError("; ".join(ctx.refused))


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(ledger_blockers, "autonomy", lambda slug: "full")
    store = SimpleNamespace(config=lambda slug: SimpleNamespace(autonomy="full"))
    return store, Ledger()


def args(action, **fields):
    base = {"tried": "", "ran": "", "output": "", "reason": "", "evidence": ""}
    return SimpleNamespace(slug="sw", action=action, item=ITEM, **{**base, **fields})


def tried(n):
    return args("attempt", tried=f"try {n}", ran=f"cmd {n}", output=f"out {n}")


def last(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_steps_are_counted_and_the_critic_gets_only_the_blind_prompt(world, capsys):
    store, ledger = world
    seen = []
    resolve.run(store, tried(1), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.ATTEMPT
    resolve.run(store, tried(2), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.CRITIC
    resolve.run(store, args("critic"), "eng", ledger, critic=lambda p: seen.append(p) or ("fake critic", "scale it"))
    shown = last(capsys)
    assert (shown["critic"], shown["next"], shown["autonomy"]) == ("scale it", ledger_blockers.ATTEMPT, "full")
    (prompt,) = seen
    assert "try 1" in prompt and "cmd 2" in prompt and "Deploy the hive" in prompt
    resolve.run(store, tried(3), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.ESCALATE
    resolve.run(store, args("escalate", reason="The key is missing everywhere.", evidence="vault ls"), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.ESCALATED
    assert [p["item"] for p in ledger.doc["priorities"]] == [ITEM]


def test_the_critic_is_refused_before_two_attempts(world):
    store, ledger = world
    resolve.run(store, tried(1), "eng", ledger)
    with pytest.raises(SwarmError, match="attempt 2 of 2"):
        resolve.run(store, args("critic"), "eng", ledger, critic=lambda p: ("c", "r"))


def test_an_attempt_needs_its_command_and_output(world):
    store, ledger = world
    with pytest.raises(SwarmError, match="--command --output"):
        resolve.run(store, args("attempt", tried="x"), "eng", ledger)


def test_an_unknown_item_is_refused(world):
    store, ledger = world
    with pytest.raises(SwarmError, match="no item tasks/t9"):
        resolve.run(store, SimpleNamespace(**{**vars(tried(1)), "item": "tasks/t9"}), "eng", ledger)


def test_a_block_at_full_waits_on_the_protocol_and_assist_does_not():
    task = {"id": "t1"}
    assert "resolve attempt tasks/t1" in resolve.block_refusal("full", "sw", task)
    assert resolve.block_refusal("assist", "sw", task) == ""
    done = {"attempts": [{}, {}, {"after_critic": True}], "critic": {"resolution": "r"}, "escalation": {"reason": "r"}}
    assert resolve.block_refusal("delegate", "sw", {**task, "blocker": done}) == ""
