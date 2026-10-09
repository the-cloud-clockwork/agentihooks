import json

import pytest

from scripts.gates import progress, talk
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger
from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "talk-2026-01-01"
ENG, CI, MASTER = "engineer@abcdef-0001", "ci@abcdef-0002", "master@abcdef-0003"


@pytest.fixture
def redis(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "ledgers")
    core.LEDGER_DIR.mkdir()
    client = fakeredis.FakeRedis(decode_responses=True)
    RedisStore(client).create(SwarmConfig(SLUG, "repo", 1, 1))
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    return client


def mode(redis, chosen):
    RedisStore(redis).update(SLUG, gates={"talk": chosen})


def budget(redis, home):
    return talk.Budget(SLUG, connect=lambda: redis, home=home)


def say(redis, home, by=ENG, n=0, **extra):
    op = {"op": "add", "thread": "chat", "id": f"m-{by}-{n}", "text": f"note {n}", "by": by, "to": "operator", **extra}
    return core.sync(SLUG, ops=[op], gate=budget(redis, home))


def outcome(redis, home, by=ENG, fields=None):
    core.sync(SLUG, ops=[{"op": "task_add", "id": "add-t1", "by": "swarm", "task": "t1", "title": "t", "lane": "eng"}])
    op = {"op": "task_update", "id": f"u-{by}", "by": by, "item": "tasks/t1", "fields": fields or {"state": "pr"}}
    return core.sync(SLUG, ops=[op], gate=budget(redis, home))


def gate_rows(home):
    path = home / SLUG / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fill(redis, home, by=ENG, count=talk.BUDGET):
    for n in range(count):
        _, rejected = say(redis, home, by, n)
        assert rejected == []


def test_enforce_refuses_the_write_past_the_budget(redis, tmp_path):
    mode(redis, "enforce")
    fill(redis, tmp_path)
    state, rejected = say(redis, tmp_path, n=99)
    assert rejected == [f"m-{ENG}-99"]
    assert state["_meta"]["warnings"] == [talk.refusal(ENG, talk.BUDGET, SLUG)]
    assert "note 99" not in [m["text"] for m in state["chat"]]
    assert progress.Progress(redis, SLUG).read(ENG).talk == talk.BUDGET
    assert [(r["gate"], r["kind"], r["agent"], r["tool"]) for r in gate_rows(tmp_path)] == [
        ("talk", "deny", ENG, "ledger")
    ]


def test_the_refusal_names_the_count_the_limit_and_the_clearing_command():
    text = talk.refusal(ENG, 10, SLUG)
    assert text.startswith(f"talk refused: {ENG} made 10 talk writes since its last outcome, the budget is 10.")
    assert f"agentihooks swarm {SLUG} pr <url>" in text
    assert "lift the talk gate" in text


def test_the_write_at_the_budget_passes(redis, tmp_path):
    mode(redis, "enforce")
    fill(redis, tmp_path, count=talk.BUDGET - 1)
    _, rejected = say(redis, tmp_path, n=50)
    assert rejected == []
    assert gate_rows(tmp_path) == []


def test_observe_lets_the_write_through_and_logs_the_would_be_deny(redis, tmp_path):
    fill(redis, tmp_path)
    state, rejected = say(redis, tmp_path, n=99)
    assert rejected == []
    assert "note 99" in [m["text"] for m in state["chat"]]
    assert [r["kind"] for r in gate_rows(tmp_path)] == ["observe"]
    assert progress.Progress(redis, SLUG).read(ENG).talk == talk.BUDGET + 1


def test_off_neither_counts_nor_logs(redis, tmp_path):
    mode(redis, "off")
    fill(redis, tmp_path, count=talk.BUDGET + 2)
    assert progress.Progress(redis, SLUG).read(ENG).talk == 0
    assert gate_rows(tmp_path) == []


def test_an_outcome_resets_the_count(redis, tmp_path):
    mode(redis, "enforce")
    fill(redis, tmp_path)
    _, rejected = outcome(redis, tmp_path)
    assert rejected == []
    assert progress.Progress(redis, SLUG).read(ENG).outcome == "task pr"
    _, rejected = say(redis, tmp_path, n=99)
    assert rejected == []


@pytest.mark.parametrize(
    ("fields", "kind"),
    [
        ({"state": "done"}, "task done"),
        ({"pr_url": "https://github.com/o/r/pull/1"}, "pull request recorded"),
        ({"proof": {"command": "c", "output": "o"}}, "proof recorded"),
    ],
)
def test_state_pull_request_and_proof_are_outcomes(redis, tmp_path, fields, kind):
    outcome(redis, tmp_path, fields=fields)
    assert progress.Progress(redis, SLUG).read(ENG).outcome == kind


def test_an_issue_link_is_not_an_outcome(redis, tmp_path):
    outcome(redis, tmp_path, fields={"issue_url": "https://github.com/o/r/issues/1"})
    assert progress.Progress(redis, SLUG).read(ENG).outcome_at == 0


def test_a_refused_task_update_records_no_outcome(redis, tmp_path):
    op = {"op": "task_update", "id": "u-x", "by": ENG, "item": "tasks/missing", "fields": {"state": "pr"}}
    _, rejected = core.sync(SLUG, ops=[op], gate=budget(redis, tmp_path))
    assert rejected == ["u-x"]
    assert progress.Progress(redis, SLUG).read(ENG).outcome_at == 0


def test_comments_and_followups_count_as_talk(redis, tmp_path):
    gate = budget(redis, tmp_path)
    comment = {"op": "add", "thread": "phases/p1/comments", "id": "c1", "text": "did it", "by": CI}
    followup = {"op": "add_item", "id": "f1", "by": CI, "list": "followups", "text": "later"}
    core.sync(SLUG, ops=[comment, followup], gate=gate)
    assert progress.Progress(redis, SLUG).read(CI).talk == 2


@pytest.mark.parametrize("by", [MASTER, "operator", "swarm"])
def test_only_eng_and_ci_agents_are_counted(redis, tmp_path, by):
    mode(redis, "enforce")
    extra = {} if by == "operator" else {"by": by, "to": "operator"}
    op = {"op": "add", "thread": "chat", "id": "m-x", "text": "hello", **extra}
    for n in range(talk.BUDGET + 2):
        _, rejected = core.sync(SLUG, ops=[{**op, "id": f"m-{n}"}], gate=budget(redis, tmp_path))
        assert rejected == []
    assert progress.Progress(redis, SLUG).read(by).talk == 0


def test_talk_owed_to_the_operator_passes_uncounted(redis, tmp_path):
    mode(redis, "enforce")
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": ENG}])
    fill(redis, tmp_path)
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "q1", "text": f"@{ENG} status?"}])
    _, rejected = say(redis, tmp_path, n=99)
    assert rejected == []
    assert progress.Progress(redis, SLUG).read(ENG).talk == talk.BUDGET


def test_an_operator_lift_lets_the_write_through_for_an_hour(redis, tmp_path):
    mode(redis, "enforce")
    fill(redis, tmp_path)
    core.sync(SLUG, ops=[{"op": "gate_lift", "id": "gl1", "by": ENG, "gate": "talk"}])
    _, rejected = say(redis, tmp_path, n=99)
    assert rejected == []


def test_a_page_lift_for_the_agent_lets_the_write_through(redis, tmp_path):
    from scripts.gates import lift
    from scripts.gates.base import Who

    mode(redis, "enforce")
    fill(redis, tmp_path)
    lift.lift_agent(Who(name=CI, swarm=SLUG), "talk", tmp_path)
    assert say(redis, tmp_path, n=98)[1] != []
    lift.lift_agent(Who(name=ENG, swarm=SLUG), "talk", tmp_path)
    assert say(redis, tmp_path, n=99)[1] == []


def test_a_lift_of_another_gate_or_agent_or_an_old_lift_does_not_count():
    events = [
        {"kind": "gate lifted", "by": ENG, "gate": "identity", "at": 10_000_000},
        {"kind": "gate lifted", "by": CI, "gate": "talk", "at": 10_000_000},
        {"kind": "gate lifted", "by": ENG, "gate": "talk", "at": 10_000_000 - 3_600_001},
    ]
    assert talk.lifted({"events": events}, ENG, 10_000_000) is False
    assert talk.lifted({"events": events[2:]}, ENG, 10_000_000 - 1) is True


def test_redis_down_lets_the_write_through_with_a_fail_open_row(redis, tmp_path):
    def down():
        raise ConnectionError("refused")

    op = {"op": "add", "thread": "chat", "id": "m-down", "text": "hi", "by": ENG, "to": "operator"}
    _, rejected = core.sync(SLUG, ops=[op], gate=talk.Budget(SLUG, connect=down, home=tmp_path))
    assert rejected == []
    rows = gate_rows(tmp_path)
    assert [(r["kind"], r["reason"]) for r in rows] == [("fail-open", "ConnectionError: refused")]


def test_a_ledger_with_no_swarm_is_not_gated(redis, tmp_path):
    redis.delete(RedisStore(redis).key(SLUG, "config"))
    fill(redis, tmp_path, count=talk.BUDGET + 2)
    assert progress.Progress(redis, SLUG).read(ENG).talk == 0
    assert gate_rows(tmp_path) == []


def test_an_unknown_mode_reads_as_observe():
    assert talk.mode_of(SwarmConfig(SLUG, "r", 1, 1, gates={"talk": "loud"})) == "observe"
    assert talk.mode_of(SwarmConfig(SLUG, "r", 1, 1)) == "observe"
    assert talk.mode_of(SwarmConfig(SLUG, "r", 1, 1, gates={"talk": "enforce"})) == "enforce"


def test_the_operator_lift_arms_the_talk_gate_beside_the_condition_gates(tmp_path):
    from scripts.gates import lift
    from scripts.gates.base import Who

    posted = []
    who = Who(name=ENG, swarm=SLUG, task="t1")
    assert lift.arm_from_prompt("lift the talk gate", "sid", who, {"identity"}, tmp_path, posted.append) == ["talk"]
    assert [op["gate"] for op in posted] == ["talk"]
    assert lift.SERVER_GATES == {talk.NAME}


@pytest.mark.parametrize(("ops", "gated"), [([{"op": "ack", "id": "ack-1"}], True), (None, False), ([], False)])
def test_the_server_gates_only_requests_that_carry_ops(monkeypatch, ops, gated):
    from scripts.swarm_ledger import ledger_server

    seen, seen_changes, changes = [], [], [{"path": "phases/p1/done", "value": True}]

    def sync(slug, changes=None, ops=None, gate=None):
        seen.append(gate)
        seen_changes.append(changes)
        return {"_meta": {"members": {}, "rev": 1}, "tasks": []}, []

    monkeypatch.setattr(ledger_server.repository, "apply_ops", sync)
    monkeypatch.setattr(ledger_server, "relay_to_inbox", lambda slug, state: [])
    monkeypatch.setattr(ledger_server, "doctor_phrase", lambda slug, state: None)
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.path = f"/api/{SLUG}?view=agent"
    handler.send = lambda code, body, ctype: code
    assert handler.reply_state(SLUG, changes=changes, ops=ops) == 200
    assert [type(gate).__name__ for gate in seen] == (["Budget"] if gated else ["NoneType"])
    assert seen_changes == [changes]
    assert not gated or seen[0].slug == SLUG


@pytest.mark.parametrize(
    ("op", "counted"),
    [
        ({"op": "add", "thread": "chat"}, False),
        ({"op": "delete", "thread": "chat", "by": ENG}, False),
        ({"op": "edit", "thread": "chat", "by": ENG}, True),
        ({"op": "edit", "thread": "tasks/t1/comments", "by": ENG}, True),
        ({"op": "add", "by": ENG}, False),
    ],
)
def test_talk_is_an_agent_add_or_edit_on_chat_or_comments(op, counted):
    assert talk.talk_op(op) is counted


@pytest.mark.parametrize(
    "op",
    [{"op": "add", "fields": {"state": "pr"}}, {"op": "task_update", "fields": {}}, {"op": "task_update"}],
)
def test_only_a_task_update_with_an_outcome_field_is_an_outcome(op):
    assert talk.outcome_op(op) == ""


def test_the_refusal_reads_in_full():
    assert talk.refusal(ENG, 12, SLUG) == (
        f"talk refused: {ENG} made 12 talk writes since its last outcome, the budget is 10. "
        f"Record an outcome first: push the commit, or run agentihooks swarm {SLUG} pr <url>. "
        "Only the operator lifts it, by typing lift the talk gate in this pane."
    )


def direct(redis, home, doc, op, meta=None, at=1000):
    from types import SimpleNamespace

    ctx = SimpleNamespace(meta=meta or {"events": [], "members": {}}, at=at, refused=[])
    return budget(redis, home).apply(doc, op, ctx, lambda d, o, c: True), ctx


def at_budget(redis, by=ENG):
    marks = progress.Progress(redis, SLUG)
    for _ in range(talk.BUDGET):
        marks.talk(by)


CHAT = {"op": "add", "thread": "chat", "id": "m1", "text": "hi", "by": ENG}


def test_a_page_lift_holds_one_hour_on_the_ledger_clock(redis, tmp_path):
    from scripts.gates import lift
    from scripts.gates.base import Who

    mode(redis, "enforce")
    at_budget(redis)
    at, who = 10_000_000, Who(name=ENG, swarm=SLUG)
    lift.lift_agent(who, "talk", tmp_path, now=at / 1000 - lift.LIFT_SECONDS + 1)
    assert direct(redis, tmp_path, {"tasks": []}, CHAT, at=at)[0] is True
    lift.lift_agent(who, "talk", tmp_path, now=at / 1000 - lift.LIFT_SECONDS)
    assert direct(redis, tmp_path, {"tasks": []}, CHAT, at=at)[0] is False


def test_an_operator_event_on_a_task_the_agent_claimed_is_owed(redis, tmp_path):
    mode(redis, "enforce")
    at_budget(redis)
    members = {ENG: {}, MASTER: {"role": "orchestrator"}}
    meta = {"members": members, "events": [{"rev": 1, "by": "operator", "kind": "comment", "target": "tasks/t1"}]}
    claimed = {"tasks": [{"id": "t1", "claimed_by": ENG, "state": "claimed"}]}
    assert direct(redis, tmp_path, claimed, CHAT, meta)[0] is True
    assert direct(redis, tmp_path, {"tasks": []}, CHAT, meta)[0] is False


@pytest.mark.parametrize(
    ("tasks", "held"),
    [
        (
            [
                {"id": "t0", "claimed_by": CI, "state": "claimed"},
                {"id": "t1", "claimed_by": ENG, "state": "done"},
                {"id": "t2", "claimed_by": ENG, "state": "claimed"},
            ],
            "t2",
        ),
        ([{"id": "t1", "claimed_by": ENG, "state": "done"}], ""),
    ],
)
def test_a_deny_row_names_the_open_task_the_agent_holds(redis, tmp_path, tasks, held):
    mode(redis, "enforce")
    at_budget(redis)
    done, ctx = direct(redis, tmp_path, {"tasks": tasks}, CHAT)
    assert done is False
    assert ctx.refused == [talk.refusal(ENG, talk.BUDGET, SLUG)]
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in gate_rows(tmp_path)] == [
        ("talk", "deny", ENG, held, "ledger", talk.refusal(ENG, talk.BUDGET, SLUG))
    ]


def test_an_observe_row_carries_the_refusal(redis, tmp_path):
    at_budget(redis)
    assert direct(redis, tmp_path, {"tasks": []}, CHAT)[0] is True
    assert [(r["gate"], r["kind"], r["tool"], r["reason"]) for r in gate_rows(tmp_path)] == [
        ("talk", "observe", "ledger", talk.refusal(ENG, talk.BUDGET, SLUG))
    ]


def test_a_fail_open_row_names_the_gate_and_the_ledger(redis, tmp_path):
    def down():
        raise ConnectionError("refused")

    op = {"op": "add", "thread": "chat", "id": "m-down", "text": "hi", "by": ENG, "to": "operator"}
    core.sync(SLUG, ops=[op], gate=talk.Budget(SLUG, connect=down, home=tmp_path))
    assert [(r["gate"], r["tool"], r["agent"]) for r in gate_rows(tmp_path)] == [("talk", "ledger", ENG)]


def test_an_outcome_is_stamped_at_the_write_time(redis, tmp_path):
    op = {"op": "task_update", "id": "u1", "by": ENG, "item": "tasks/t1", "fields": {"state": "pr"}}
    assert direct(redis, tmp_path, {"tasks": []}, op, at=4242)[0] is True
    assert progress.Progress(redis, SLUG).read(ENG) == progress.Mark(4242, "task pr", 0)


def test_a_sync_reports_both_a_refused_change_and_a_refused_op(redis, tmp_path):
    mode(redis, "enforce")
    fill(redis, tmp_path)
    op = {"op": "add", "thread": "chat", "id": "m-late", "text": "late", "by": ENG}
    _, rejected = core.sync(
        SLUG, changes=[{"path": "nowhere/x/done", "value": True}], ops=[op], gate=budget(redis, tmp_path)
    )
    assert rejected == ["nowhere/x/done", "m-late"]


def refused_state(*warnings, rejected=("m1",)):
    return {"rejected": list(rejected), "_meta": {"warnings": list(warnings)}}


def test_the_cli_exits_with_every_refusal_reason():
    from scripts.swarm_ledger import ledger

    with pytest.raises(SystemExit) as stop:
        ledger.refused(refused_state("talk refused: one", "stale page", "talk refused: two"))
    assert stop.value.code == "talk refused: one; stale page; talk refused: two"


@pytest.mark.parametrize("state", [refused_state("talk refused: old", rejected=()), {"rejected": []}, {}])
def test_the_cli_passes_accepted_writes(state):
    from scripts.swarm_ledger import ledger

    assert ledger.refused(state) is None


def test_posted_prints_whether_the_write_landed(capsys):
    from scripts.swarm_ledger import ledger

    ledger.posted({"rejected": []}, [])
    assert json.loads(capsys.readouterr().out) == {"posted": True}
    with pytest.raises(SystemExit) as stop:
        ledger.posted(refused_state("stale page"), [])
    assert stop.value.code == "stale page"
    assert json.loads(capsys.readouterr().out) == {"posted": False}


def test_say_stops_on_a_talk_refusal(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm_ledger import ledger

    monkeypatch.setattr(ledger, "call", lambda slug, ops: refused_state("talk refused: busy"))
    args = SimpleNamespace(text="hi", name=ENG, long=False, slug=SLUG)
    with pytest.raises(SystemExit) as stop:
        ledger.cmd_say(args)
    assert stop.value.code == "talk refused: busy"
