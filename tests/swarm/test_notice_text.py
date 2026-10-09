import json

import pytest

from scripts.gates import claims, intent
from scripts.inbox import wake
from scripts.inbox.store import Item
from scripts.swarm import capacity, grouping, notice_text, phase_planning, tick, trace_plan
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused, SwarmError, _ledger
from scripts.swarm.store import SwarmConfig
from scripts.swarm_ledger import ledger_comments

NOISE = (
    "see scripts/swarm/tick.py at 03:45:16 UTC on 2026-10-09; run 37818444803; commit fa09b9d0 "
    "-> quota_handoff.warn (retry) — LABEL IN CAPS; delve deeper"
)
LONG = " ".join([NOISE] * 4)


def _worst_capacity():
    closed = [
        capacity.Account(harness, f"{harness}{n}", "CLOSED", 3, 0.0, 1.0, 3)
        for harness in ("claude", "codex")
        for n in range(6)
    ]
    apis = [
        capacity.Account(harness, "api", "OPEN", 0, None, None, None, kind=capacity.API, weight=3)
        for harness in ("claude", "codex")
    ]
    warned = {(row.harness, row.name): "week" for row in closed}
    config = SwarmConfig("sw", "/repo", max_eng=9, max_ci=9, max_plan=9)
    decision = capacity.calculate(config, closed + apis, [], warned=warned)
    return capacity.status_line(decision)


def _unread():
    return Item("i1", "engineer@323133-0768", "master@rig-grade-swarm", LONG, "pending", 0, 0)


def templates():
    return [
        ("capacity status", "comment", _worst_capacity()),
        ("claim cap", "comment", claims.refusal(9, NOISE, NOISE, "rig-grade-swarm", "stall1")),
        ("group proposal", "priority", grouping.PROPOSE.format(n=12)),
        ("plan followup", "item", trace_plan.followup_text("stall1", NOISE)),
        ("intent failed", "comment", intent.FAIL_COMMENT),
        ("intent shortfall", "comment", intent.SHORTFALL_COMMENT),
        ("slice check", "comment", phase_planning._comment([NOISE] * 9, 12, tail=NOISE)),
        ("unread mail", "item", wake._operator_text(_unread())),
        ("raw noise", "comment", NOISE),
        ("long noise", "comment", LONG),
        ("long noise item", "item", LONG),
        ("long noise priority", "priority", LONG),
        ("raw noise item", "item", NOISE),
        ("raw noise priority", "priority", NOISE),
        ("empty", "comment", ""),
    ]


@pytest.mark.parametrize(("name", "kind", "text"), templates(), ids=[t[0] for t in templates()])
def test_every_swarm_notice_passes_the_servers_plain_words_check(name, kind, text):
    assert ledger_comments.problems(notice_text.plain(text, kind), kind) == []


def test_the_worst_capacity_status_would_be_refused_unformatted():
    assert ledger_comments.problems(_worst_capacity(), "comment")


def test_plain_keeps_meaning_of_ordinary_text():
    assert notice_text.plain("accounts have quota; Claude has 2 free seats; Codex has 1") == (
        "accounts have quota, Claude has 2 free seats, Codex has 1"
    )


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("fixed fa09b9d0 here", "comment", "fixed here"),
        ("a ; b . c", "comment", "a, b. c"),
        ("; start;", "comment", "start"),
        ("quota->spawn and a=>b", "comment", "quota to spawn and a to b"),
        ("ready — merged – queued", "comment", "ready, merged, queued"),
        ("held (for now) by quota_handoff", "comment", "held for now by quota handoff"),
        (" ".join(["word"] * 30), "priority", " ".join(["word"] * 20)),
        (" ".join(["word"] * 60), "item", " ".join(["word"] * 40)),
        (" ".join(["word"] * 60), "comment", " ".join(["word"] * 50)),
    ],
)
def test_plain_reshapes_text_exactly(text, kind, expected):
    assert notice_text.plain(text, kind) == expected


def test_plain_falls_back_when_nothing_is_left():
    assert notice_text.plain("") == notice_text.FALLBACK


def _capture(monkeypatch, refuse=False):
    sent = []

    def call(slug, ops, service=False):
        sent.append(ops)
        if refuse:
            raise SystemExit("server refused: 400 comment refused")
        return {"tasks": []}

    monkeypatch.setattr(_ledger(), "call", call)
    return sent


@pytest.mark.parametrize(
    ("write", "kind"),
    [
        (lambda c: c.comment("sw", "t1", LONG, by="swarm"), "comment"),
        (lambda c: c.comment_phase("sw", "p1", LONG, by="swarm"), "comment"),
        (lambda c: c.comment_item("sw", "followups/f1", LONG), "comment"),
        (lambda c: c.followup("sw", LONG), "item"),
        (lambda c: c.priority("sw", "tasks/t1", LONG), "priority"),
    ],
)
def test_client_formats_swarm_notices_through_the_server_check(monkeypatch, write, kind):
    sent = _capture(monkeypatch)
    write(LedgerClient())
    (op,) = sent[0]
    assert op["text"] == notice_text.plain(LONG, kind)
    assert ledger_comments.problems(op["text"], kind) == []


def test_client_keeps_an_agents_own_comment_as_written(monkeypatch):
    sent = _capture(monkeypatch)
    LedgerClient().comment("sw", "t1", "done; merged", by="engineer@1-2")
    assert sent[0][0]["text"] == "done; merged"


def test_client_logs_and_drops_a_refused_swarm_notice(monkeypatch, capsys):
    _capture(monkeypatch, refuse=True)
    assert LedgerClient().comment("sw", "t1", "hello", by="swarm") is None
    assert "swarm notice dropped, the ledger refused it" in capsys.readouterr().err


def test_client_still_raises_a_refused_agent_comment(monkeypatch):
    _capture(monkeypatch, refuse=True)
    with pytest.raises(LedgerRefused):
        LedgerClient().comment("sw", "t1", "hello", by="engineer@1-2")


class _Redis(dict):
    def set(self, key, value):
        self[key] = value

    def get(self, key):
        return dict.get(self, key)

    def hget(self, key, field):
        return None


class _Store:
    def __init__(self):
        self.redis = _Redis()

    def key(self, slug, name):
        return f"{slug}:{name}"

    def agents(self, slug):
        return []


class _RefusingLedger:
    def state(self, slug):
        return {"tasks": [{"id": "t1", "state": "done", "done": True}]}

    def comment(self, slug, task_id, text, by):
        raise LedgerRefused("refused")


class _Runtime:
    def quota_capacity(self, config, agents, now, demand, requirements):
        return {
            "configured": {"eng": 1, "ci": 0, "plan": 0},
            "effective": {"eng": 1, "ci": 0, "plan": 0},
            "reason": "accounts have quota",
            "placements": {},
        }


def test_capacity_saves_its_decision_and_drops_a_refused_notice(monkeypatch):
    monkeypatch.setattr("scripts.swarm.tick._claimable", lambda *a: [])
    store = _Store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    actions = capacity.apply("sw", config, store, _RefusingLedger(), _Runtime(), 1000)
    assert actions == [capacity.status_line(json.loads(store.redis["sw:quota-capacity"]))]


class _CapStore:
    def __init__(self):
        self.lives = claims.CAP

    def claims(self, slug, task_id):
        return self.lives

    def config(self, slug):
        return SwarmConfig("sw", "/repo", max_eng=1, max_ci=1)

    def handoff_envelope(self, slug, task_id):
        return None

    def launch_failure(self, slug, task_id):
        return None

    def reset_claims(self, slug, task_id):
        self.lives = 0


class _CapLedger:
    def __init__(self, store):
        self.store, self.seen = store, []

    def update_task(self, slug, task_id, fields, if_state=()):
        return {"id": task_id, "state": "blocked"}

    def comment(self, slug, task_id, text, by):
        self.seen.append(self.store.lives)
        raise SwarmError("ledger sw: connection reset")


def test_claim_cap_resets_claims_before_its_notice(monkeypatch):
    monkeypatch.setattr(tick.gate_log, "append", lambda *a, **k: None)
    store = _CapStore()
    ledger = _CapLedger(store)
    with pytest.raises(SwarmError):
        tick._claim_cap("sw", store, ledger, {"t1": {"id": "t1"}}, {"id": "t1"})
    assert (ledger.seen, store.lives) == ([0], 0)
