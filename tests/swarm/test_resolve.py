import json
from types import SimpleNamespace

import pytest

from scripts.headless import runner
from scripts.headless.request import HeadlessError
from scripts.swarm import ledger_client, resolve
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_blockers

pytestmark = pytest.mark.unit

ITEM = "tasks/t1"
NEXT_ATTEMPT = (
    'agentihooks swarm sw resolve attempt tasks/t1 --tried "<what you tried>" --command "<the command run>" '
    '--output "<its output>"'
)


class Ledger:
    def __init__(self):
        self.doc = {"tasks": [{"id": "t1", "title": "Deploy the hive", "comments": []}], "priorities": []}
        self.writes = []

    def item(self, slug, path):
        assert slug == "sw"
        return ledger_blockers.find(self.doc, path)

    def blocker(self, slug, kind, by, item, fields):
        assert slug == "sw"
        op = {"op": kind, "id": f"{kind}-{len(self.writes)}", "by": by, "item": item, **fields}
        self.writes.append(op)
        ledger_blockers.check(op)
        ctx = SimpleNamespace(slug=slug, at=1, refused=[], record=lambda *a, **k: None, stamp=lambda *a: None)
        if not ledger_blockers.apply(self.doc, op, ctx):
            raise SwarmError("; ".join(ctx.refused))


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(ledger_blockers, "autonomy", lambda slug: "full")

    def config(slug):
        assert slug == "sw"
        return SimpleNamespace(autonomy="full")

    return SimpleNamespace(config=config), Ledger()


def args(action, **fields):
    base = {"tried": "", "ran": "", "output": "", "reason": "", "evidence": ""}
    return SimpleNamespace(slug="sw", action=action, item=ITEM, **{**base, **fields})


def tried(n):
    return args("attempt", tried=f"try {n}", ran=f"cmd {n}", output=f"out {n}")


def last(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_status_prints_the_counted_record_and_writes_nothing(world, capsys):
    store, ledger = world
    resolve.run(store, args("status"), "eng", ledger)
    assert last(capsys) == {
        "item": ITEM,
        "autonomy": "full",
        "attempts": 0,
        "critic": None,
        "next": "attempt",
        "do": f"attempt 1 of 2: {NEXT_ATTEMPT}",
    }
    assert ledger.writes == []


def test_the_steps_are_counted_and_the_critic_gets_only_the_blind_prompt(world, capsys):
    store, ledger = world
    seen = []
    resolve.run(store, tried(1), "eng", ledger)
    assert ledger.writes[0] == {
        "op": "blocker_attempt",
        "id": "blocker_attempt-0",
        "by": "eng",
        "item": ITEM,
        "text": "try 1",
        "command": "cmd 1",
        "output": "out 1",
    }
    assert last(capsys)["attempts"] == 1
    resolve.run(store, tried(2), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.CRITIC
    resolve.run(store, args("critic"), "eng", ledger, critic=lambda p: seen.append(p) or ("fake critic", "scale it"))
    shown = last(capsys)
    assert (shown["critic"], shown["next"], shown["autonomy"]) == ("scale it", ledger_blockers.ATTEMPT, "full")
    (prompt,) = seen
    attempts = ledger.doc["tasks"][0]["blocker"]["attempts"][:2]
    assert prompt == ledger_blockers.blind_prompt(ITEM, "Deploy the hive", "full", attempts)
    assert ledger.writes[-1]["digest"] == ledger_blockers.digest(prompt)
    assert (ledger.writes[-1]["run"], ledger.writes[-1]["resolution"]) == ("fake critic", "scale it")
    resolve.run(store, tried(3), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.ESCALATE
    resolve.run(store, args("escalate", reason="The key is missing everywhere.", evidence="vault ls"), "eng", ledger)
    assert last(capsys)["next"] == ledger_blockers.ESCALATED
    assert [p["item"] for p in ledger.doc["priorities"]] == [ITEM]


def test_the_critic_is_refused_before_two_attempts(world):
    store, ledger = world
    resolve.run(store, tried(1), "eng", ledger)
    with pytest.raises(SwarmError, match="^attempt 2 of 2: agentihooks swarm sw resolve attempt tasks/t1 "):
        resolve.run(store, args("critic"), "eng", ledger, critic=lambda p: ("c", "r"))


def test_an_attempt_needs_its_command_and_output(world):
    store, ledger = world
    with pytest.raises(SwarmError, match="needs --command --output$"):
        resolve.run(store, args("attempt", tried="x"), "eng", ledger)
    with pytest.raises(SwarmError, match="needs --tried$"):
        resolve.run(store, args("attempt", ran="ls", output="ok"), "eng", ledger)


def test_an_unknown_item_is_refused(world):
    store, ledger = world
    with pytest.raises(SwarmError, match="no item tasks/t9"):
        resolve.run(store, SimpleNamespace(**{**vars(tried(1)), "item": "tasks/t9"}), "eng", ledger)


def test_a_block_at_full_waits_on_the_protocol_and_assist_does_not():
    task = {"id": "t1"}
    assert resolve.block_refusal("full", "sw", task) == (
        "at full autonomy a block reaches the operator only after the blocker protocol, so the next step is to "
        f"attempt 1 of 2: {NEXT_ATTEMPT}"
    )
    assert resolve.block_refusal("assist", "sw", task) == ""
    done = {"attempts": [{}, {}, {"after_critic": True}], "critic": {"resolution": "r"}, "escalation": {"reason": "r"}}
    assert resolve.block_refusal("delegate", "sw", {**task, "blocker": done}) == ""


def test_the_headless_critic_is_read_only_and_answers_one_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a: calls.append(a) or {"resolution": "scale it"})
    assert resolve.headless_critic("the prompt") == ("headless high tier read only critic", "scale it")
    assert calls == [(resolve.CRITIC_PROFILE, "the prompt", resolve.CRITIC_SCHEMA, "read-only", "high", "medium")]


def test_a_critic_that_does_not_answer_is_a_swarm_error(monkeypatch):
    def down(*a):
        raise HeadlessError("claude exited 1")

    monkeypatch.setattr(runner, "run", down)
    with pytest.raises(SwarmError, match="^the blind critic did not answer: claude exited 1$"):
        resolve.headless_critic("p")


def test_the_ledger_client_reads_one_item_and_writes_one_step(monkeypatch):
    client, reads, calls = ledger_client.LedgerClient(), [], []
    monkeypatch.setattr(client, "_resource", lambda slug, path: reads.append((slug, path)) or {"id": "a/b"})
    monkeypatch.setattr(client, "_call", lambda slug, ops: calls.append((slug, ops)))
    assert client.item("sw", "followups/a/b") == {"id": "a/b"}
    assert reads == [("sw", "followups/a%2Fb")]
    client.blocker("sw", "blocker_attempt", "eng", ITEM, {"text": "x"})
    ((slug, (op,)),) = calls
    assert (slug, {k: v for k, v in op.items() if k != "id"}) == (
        "sw",
        {"op": "blocker_attempt", "by": "eng", "item": ITEM, "text": "x"},
    )
