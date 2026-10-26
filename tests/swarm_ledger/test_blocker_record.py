import re

import pytest

from scripts.swarm_ledger import ledger_blockers as blockers
from scripts.swarm_ledger import ledger_freezes

pytestmark = pytest.mark.unit

SLUG, ITEM, BY = "sw", "tasks/t1", "eng@sw"
ATTEMPT_STEP = (
    'agentihooks swarm sw resolve attempt tasks/t1 --tried "<what you tried>" --command "<the command run>" '
    '--output "<its output>"'
)
CRITIC_STEP = "launch the blind critic: agentihooks swarm sw resolve critic tasks/t1"
ESCALATE_STEP = (
    "escalate with the blocker reason and its evidence: agentihooks swarm sw resolve escalate tasks/t1 "
    '--reason "<why it is blocked, in plain words>" --evidence "<the command and output that show it>"'
)
DONE_STEP = "leave it for the operator, since its protocol is complete"
REASON = "The pool quota is spent."


class Ctx:
    def __init__(self):
        self.slug, self.at, self.refused, self.events, self.stamps = SLUG, 7, [], [], {}

    def record(self, by, kind, target, **extra):
        self.events.append((by, kind, target, extra))

    def stamp(self, path, by):
        self.stamps[path] = by


@pytest.fixture
def tier(monkeypatch):
    held = {"tier": "full"}
    monkeypatch.setattr(blockers, "autonomy", lambda slug: held["tier"] if slug == SLUG else "")
    return held


def ledger():
    other = {"id": "p0", "item": "tasks/t9", "text": "Other.", "by": "x", "at": 1}
    old = {"id": "p1", "item": ITEM, "text": "Old.", "by": "x", "at": 1}
    return {"tasks": [{"id": "t1", "title": "Deploy the hive"}], "priorities": [other, old]}


def op(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": BY, "item": ITEM, **fields}


def attempt(n):
    return op("blocker_attempt", n, text=f"tried {n}", command=f"cmd {n}", output=f"out {n}")


def critic(doc, n=1, tier="full"):
    attempts = blockers.record(blockers.find(doc, ITEM))["attempts"]
    prompt = blockers.blind_prompt(ITEM, "Deploy the hive", tier, attempts)
    return op("blocker_critic", n, run="headless critic", digest=blockers.digest(prompt), resolution="scale the pool")


def escalate(n=1, reason=REASON):
    return op("blocker_escalate", n, reason=reason, evidence="quota show")


def write(doc, change):
    ctx = Ctx()
    blockers.check(change)
    return blockers.apply(doc, change, ctx), ctx


def recorded(n, after):
    return {
        "id": f"blocker_attempt-{n}",
        "by": BY,
        "at": 7,
        "text": f"tried {n}",
        "command": f"cmd {n}",
        "output": f"out {n}",
        "after_critic": after,
    }


def through_critic(doc):
    write(doc, attempt(1))
    write(doc, attempt(2))
    write(doc, critic(doc))
    write(doc, attempt(3))


def test_each_step_is_recorded_with_its_event_and_stamp(tier):
    doc = ledger()
    done, ctx = write(doc, attempt(1))
    assert done is True
    assert ctx.events == [(BY, "blocker attempt", ITEM, {"id": "blocker_attempt-1", "text": "tried 1"})]
    assert ctx.stamps == {ITEM: BY}
    write(doc, attempt(2))
    criticized = critic(doc)
    done, ctx = write(doc, criticized)
    assert done is True
    assert ctx.events == [(BY, "blocker critic", ITEM, {"id": "blocker_critic-1", "text": "scale the pool"})]
    done, _ = write(doc, attempt(3))
    record = doc["tasks"][0]["blocker"]
    assert record["attempts"] == [recorded(1, False), recorded(2, False), recorded(3, True)]
    assert record["critic"] == {
        "id": "blocker_critic-1",
        "by": BY,
        "at": 7,
        "run": "headless critic",
        "digest": criticized["digest"],
        "resolution": "scale the pool",
    }
    assert record["escalation"] is None


def test_the_escalation_lands_the_priority_and_one_comment(tier):
    doc = ledger()
    through_critic(doc)
    done, ctx = write(doc, escalate())
    assert done is True
    text = (
        "Blocker protocol record.\n\n"
        "Attempt 1: tried 1\nCommand: cmd 1\nOutput: out 1\n\n"
        "Attempt 2: tried 2\nCommand: cmd 2\nOutput: out 2\n\n"
        "Attempt 3 after the critic: tried 3\nCommand: cmd 3\nOutput: out 3\n\n"
        "Critic resolution (headless critic): scale the pool\n\n"
        f"Blocker reason: {REASON}\nEvidence: quota show"
    )
    row = doc["tasks"][0]
    assert row["comments"] == [{"id": "blocker_escalate-1-record", "by": BY, "at": 7, "text": text}]
    assert doc["priorities"] == [
        {"id": "p0", "item": "tasks/t9", "text": "Other.", "by": "x", "at": 1},
        {"id": "blocker_escalate-1", "item": ITEM, "text": f"Blocked: {REASON}", "by": BY, "at": 7},
    ]
    assert row["blocker"]["escalation"] == {
        "id": "blocker_escalate-1",
        "by": BY,
        "at": 7,
        "reason": REASON,
        "evidence": "quota show",
    }
    assert ctx.events == [
        (BY, "comment added", ITEM, {"id": "blocker_escalate-1-record", "text": text}),
        (BY, "priority added", ITEM, {"id": "blocker_escalate-1", "text": f"Blocked: {REASON}"}),
    ]
    assert ctx.stamps == {ITEM: BY}
    assert blockers.next_step(row["blocker"]) == blockers.ESCALATED


@pytest.mark.parametrize(
    "steps, change, refusal",
    [
        (2, attempt(3), f"two attempts are recorded before the critic; {CRITIC_STEP}"),
        (1, "critic", f"the critic runs once, after two attempts; attempt 2 of 2: {ATTEMPT_STEP}"),
        (2, "forged", f"the critic record does not match the code built blind prompt; {CRITIC_STEP}"),
        (3, "critic", f"the critic runs once, after two attempts; apply the critic resolution: {ATTEMPT_STEP}"),
        (4, attempt(5), f"one attempt applies the critic resolution; {ESCALATE_STEP}"),
        (0, escalate(), f"at full autonomy the item escalates only after the protocol; attempt 1 of 2: {ATTEMPT_STEP}"),
        (5, escalate(2), f"at full autonomy the item has already escalated; {DONE_STEP}"),
    ],
)
def test_each_out_of_order_step_is_refused_with_the_next_step(tier, steps, change, refusal):
    doc = ledger()
    for step in [attempt(1), attempt(2), "critic", attempt(3), escalate()][:steps]:
        write(doc, critic(doc) if step == "critic" else step)
    before = {**doc["tasks"][0]}
    if change == "critic":
        change = critic(doc, 2)
    elif change == "forged":
        change = {**critic(doc, 2), "digest": "0" * 64}
    done, ctx = write(doc, change)
    assert (done, ctx.refused, ctx.events, ctx.stamps) == (False, [refusal], [], {})
    assert doc["tasks"][0] == before


def test_the_critic_digest_carries_the_tier_the_ledger_reads(tier):
    doc = ledger()
    write(doc, attempt(1))
    write(doc, attempt(2))
    done, ctx = write(doc, critic(doc, tier="assist"))
    assert (done, ctx.refused) == (
        False,
        [f"the critic record does not match the code built blind prompt; {CRITIC_STEP}"],
    )


def test_below_delegate_an_escalation_needs_no_record(tier):
    tier["tier"] = "assist"
    doc = {"tasks": [{"id": "t1", "title": "Deploy the hive"}]}
    done, _ = write(doc, escalate())
    assert done is True
    assert [p["text"] for p in doc["priorities"]] == [f"Blocked: {REASON}"]
    assert (
        doc["tasks"][0]["comments"][0]["text"]
        == f"Blocker protocol record.\n\nBlocker reason: {REASON}\nEvidence: quota show"
    )


def test_an_item_the_ledger_lacks_is_not_written(tier):
    doc = ledger()
    done, ctx = write(doc, {**attempt(1), "item": "tasks/t9"})
    assert (done, ctx.events, ctx.stamps) == (False, [], {})


def test_the_ledger_autonomy_is_the_swarm_config_read(monkeypatch):
    monkeypatch.setattr(ledger_freezes, "autonomy", lambda slug: f"tier of {slug}")
    assert blockers.autonomy("sw") == "tier of sw"


@pytest.mark.parametrize(
    "change, message",
    [
        ({**attempt(1), "extra": "x"}, "blocker_attempt takes only by item text command output"),
        ({**escalate(), "run": "x"}, "blocker_escalate takes only by item reason evidence"),
        ({**attempt(1), "by": "operator"}, "blocker_attempt is written by an agent"),
        ({**attempt(1), "by": "9bad"}, "blocker_attempt is written by an agent"),
        ({**attempt(1), "by": 3}, "blocker_attempt is written by an agent"),
        ({**attempt(1), "item": "tasks/t1/comments"}, "blocker_attempt needs item <list>/<id>"),
        ({**attempt(1), "text": None}, "blocker_attempt needs text as text of up to 8000 characters"),
        ({**attempt(1), "command": "  "}, "blocker_attempt needs command as text of up to 8000 characters"),
        ({**attempt(1), "output": "x" * 8001}, "blocker_attempt needs output as text of up to 8000 characters"),
        (
            {**op("blocker_critic", 1, run="r", resolution="x"), "digest": "abc"},
            "blocker_critic digest is the sha256 of the blind prompt",
        ),
    ],
)
def test_malformed_steps_are_refused_by_check(change, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        blockers.check(change)


def test_the_longest_texts_and_the_escalation_text_rules_hold():
    blockers.check({**attempt(1), "output": "x" * 8000})
    blockers.check(critic({"tasks": [{"id": "t1"}]}))
    with pytest.raises(ValueError):
        blockers.check(escalate(reason="Merged 4a5414f78 at 19:30Z."))


def test_the_priority_text_keeps_fourteen_words():
    fourteen = " ".join(f"w{n}" for n in range(14))
    assert blockers._priority_text(fourteen) == f"Blocked: {fourteen}"
    assert blockers._priority_text(fourteen + " w14") == f"Blocked: {fourteen}…"


def test_the_blind_prompt_and_its_subject():
    attempts = [recorded(1, False), recorded(2, True)]
    assert blockers.blind_prompt(ITEM, "Deploy", "", attempts) == (
        f"{blockers.CRITIC_ROLE}\n\nItem: tasks/t1\nProblem: Deploy\nAutonomy: unknown\n\n"
        "Attempt 1: tried 1\nCommand: cmd 1\nOutput: out 1\n\n"
        "Attempt 2 after the critic: tried 2\nCommand: cmd 2\nOutput: out 2"
    )
    assert [blockers.subject(row) for row in ({"title": "T", "text": "x"}, {"text": "x"}, {}, None)] == [
        "T",
        "x",
        "",
        "",
    ]


def test_steps_and_notices_name_the_next_command():
    rec = blockers.record({})
    assert blockers.step_text(SLUG, ITEM, rec) == f"attempt 1 of 2: {ATTEMPT_STEP}"
    tried = {**rec, "attempts": [{}, {}], "critic": {"resolution": "r"}}
    assert blockers.step_text(SLUG, ITEM, tried) == f"apply the critic resolution: {ATTEMPT_STEP}"
    assert blockers.step_text(SLUG, ITEM, {**tried, "attempts": [{}, {}, {"after_critic": True}]}) == ESCALATE_STEP
    assert blockers.notice(SLUG, "full", ITEM, rec) == (
        "Autonomy full: take the decision yourself, with no question to the operator. "
        f"On a blocker, the next protocol step is to attempt 1 of 2: {ATTEMPT_STEP}."
    )
    assert blockers.notice(SLUG, "", ITEM, rec) == (
        "Autonomy unknown. Triage it: resolve it if the call is yours, else leave it for the operator."
    )


def test_find_reads_one_list_row():
    doc = ledger()
    assert blockers.find(doc, ITEM) == {"id": "t1", "title": "Deploy the hive"}
    assert blockers.find(doc, "tasks/t9") is None
    assert blockers.find({}, "phases/p1") is None


def test_the_asked_item_of_each_operator_facing_write():
    flag = {"op": "set", "path": "followups/f1/needs_operator", "value": True}
    assert blockers.asked_item({"op": "priority", "item": ITEM}) == ITEM
    assert blockers.asked_item(flag) == "followups/f1"
    assert blockers.asked_item({**flag, "path": "followups/f1/done"}) == ""
    assert blockers.asked_item({"op": "add_item", "list": "questions"}) == ""


class Inner:
    def __init__(self):
        self.calls = []

    def apply(self, doc, op, ctx, apply):
        self.calls.append((doc, op, ctx, apply))
        return "inner"


def test_the_gate_passes_allowed_writes_to_its_inner_gate(tier):
    inner, doc, ctx = Inner(), ledger(), Ctx()
    change = {"op": "priority", "id": "p", "by": "swarm", "item": ITEM, "text": "Ask."}
    assert blockers.Gate(SLUG, inner).apply(doc, change, ctx, print) == "inner"
    assert inner.calls == [(doc, change, ctx, print)]


@pytest.mark.parametrize(
    "change, refusal",
    [
        (
            {"op": "priority", "id": "p", "by": "boss", "item": ITEM, "text": "Ask."},
            "at full autonomy boss takes the decision itself; an item reaches the operator only after the blocker "
            f"protocol, so the next step is to attempt 1 of 2: {ATTEMPT_STEP}",
        ),
        (
            {"op": "add_item", "id": "q", "by": "boss", "list": "questions", "text": "Which?"},
            "at full autonomy boss takes the decision itself; an item reaches the operator only after the blocker "
            "protocol of the existing item it concerns, never a new question or follow up, so the next step is to "
            "attempt 1 of 2: " + ATTEMPT_STEP.replace("tasks/t1", "<the item it concerns>"),
        ),
    ],
)
def test_the_gate_refuses_an_ask_without_the_protocol(tier, change, refusal):
    inner, ctx = Inner(), Ctx()
    assert blockers.Gate(SLUG, inner).apply(ledger(), change, ctx, print) is False
    assert (ctx.refused, inner.calls) == ([refusal], [])


def test_the_gate_lets_an_escalated_item_and_lower_tiers_through(tier):
    doc = ledger()
    through_critic(doc)
    write(doc, escalate())
    change = {"op": "priority", "id": "p", "by": "boss", "item": ITEM, "text": "Ask."}
    gate = blockers.Gate(SLUG)
    assert gate.refusal(doc, change) == ""
    tier["tier"] = "assist"
    assert gate.refusal(ledger(), change) == ""
    assert blockers.Gate("other").refusal(ledger(), change) == ""
