import json

import pytest

from scripts.gates import progress, talk
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger

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
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    return client


def mode(redis, chosen):
    RedisStore(redis).update(SLUG, gates={"talk": chosen})


def budget(redis, home):
    return talk.Budget(SLUG, connect=lambda: redis, home=home)


def say(redis, home, by=ENG, n=0, **extra):
    op = {"op": "add", "thread": "chat", "id": f"m-{by}-{n}", "text": f"note {n}", "by": by, **extra}
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
    extra = {} if by == "operator" else {"by": by}
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

    op = {"op": "add", "thread": "chat", "id": "m-down", "text": "hi", "by": ENG}
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


def test_the_cli_and_the_gate_share_the_refusal_prefix():
    from scripts.swarm_ledger import ledger

    assert ledger.TALK_REFUSED == talk.REFUSED


@pytest.mark.parametrize(("ops", "gated"), [([{"op": "ack"}], True), (None, False), ([], False)])
def test_the_server_gates_only_requests_that_carry_ops(monkeypatch, ops, gated):
    from scripts.swarm_ledger import ledger_server

    seen = []

    def sync(slug, changes=None, ops=None, gate=None):
        seen.append(gate)
        return {"_meta": {"members": {}}, "tasks": []}, []

    monkeypatch.setattr(ledger_server.core, "sync", sync)
    monkeypatch.setattr(ledger_server, "relay_to_inbox", lambda slug, state: [])
    monkeypatch.setattr(ledger_server, "doctor_phrase", lambda slug, state: None)
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.path = f"/api/{SLUG}?view=agent"
    handler.send = lambda code, body, ctype: code
    assert handler.reply_state(SLUG, ops=ops) == 200
    assert [type(gate).__name__ for gate in seen] == (["Budget"] if gated else ["NoneType"])
    assert not gated or seen[0].slug == SLUG
