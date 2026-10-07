import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import quota_view
from scripts.swarm.health.findings import Finding, Limits, limits
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "s"
MINUTE = 60_000


def _store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def row(name, state="NORMAL", sessions=0, five=80.0, week=60.0, harness="claude"):
    return {
        "harness": harness,
        "name": name,
        "state": state,
        "sessions": sessions,
        "five_left": five,
        "week_left": week,
    }


DECISION = {
    "configured": {"eng": 4, "ci": 1, "plan": 1},
    "effective": {"eng": 2, "ci": 1, "plan": 0},
    "placeable": {"claude": 1, "codex": 0},
    "reason": "accounts are drain; Claude has 1 free seats and Codex has 0 free seats",
    "accounts": [
        row("alpha", "DRAIN", 2, 40.0, 7.5),
        row("beta", sessions=1),
        row("main", "UNKNOWN", 0, None, None, "codex"),
    ],
    "at": 1_000_000,
}


def test_status_lines_name_each_lane_cap_the_reason_the_change_age_and_every_account():
    assert quota_view.lines(DECISION, 1_000_000 + 5 * MINUTE) == [
        "quota capacity eng 2 of 4, ci 1 of 1, plan 0 of 1, changed 5 minutes ago, because accounts are drain; "
        "Claude has 1 free seats and Codex has 0 free seats",
        "quota account claude alpha  drain  routing 7.5% left  sessions 2",
        "quota account claude beta  normal  routing 60% left  sessions 1",
        "quota account codex main  unknown  routing unknown  sessions 0",
    ]


def test_a_change_under_a_minute_old_reads_just_now_and_one_minute_is_singular():
    assert quota_view.lines({**DECISION, "accounts": []}, 1_000_000 + 59_999)[0].split(", ")[3] == "changed just now"
    assert (
        quota_view.lines({**DECISION, "accounts": []}, 1_000_000 + MINUTE)[0].split(", ")[3] == "changed 1 minute ago"
    )


def test_no_decision_says_capacity_was_not_observed():
    assert quota_view.lines({}, 0) == ["quota capacity has not been observed"]


def test_routing_left_is_the_lower_window_or_none_when_either_is_unknown():
    assert quota_view.routing_left(row("a", five=30.0, week=70.0)) == 30.0
    assert quota_view.routing_left(row("a", five=90.0, week=12.0)) == 12.0
    assert quota_view.routing_left(row("a", five=None, week=12.0)) is None
    assert quota_view.routing_left(row("a", five=12.0, week=None)) is None


def _drain_swarm(
    state="DRAIN", agent_state="working", idle_ticks=0, warned_at=1_000_000, harness="claude", store=None, slug=SLUG
):
    store = store or _store()
    store.redis.set(
        store.key(slug, "quota-capacity"),
        json.dumps({**DECISION, "accounts": [row("alpha", state, 1, 40.0, 7.5, harness)]}),
    )
    store.put_agent(
        slug,
        AgentRecord(
            "engineer@abc-0001", "eng", "t1", harness=harness, account="alpha", state=agent_state, idle_ticks=idle_ticks
        ),
    )
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", "engineer@abc-0001", "QUOTA HANDOFF WARNING")
    store.redis.hset(inbox.key("item", item.id), "created_at", warned_at)
    store.redis.hset(store.key(slug, "quota-warnings"), "engineer@abc-0001", item.id)
    return store


def test_an_agent_still_working_on_a_draining_account_past_its_warning_is_a_finding():
    store = _drain_swarm()
    found = quota_view.findings(store, SLUG, Limits(), 1_000_000 + 12 * MINUTE)
    assert found == [
        Finding(
            "working on drain",
            "engineer@abc-0001",
            "still working on claude account alpha 12 minutes after its quota handoff warning",
            ("account alpha is drain, routing 7.5% left", "task t1"),
            "10 minutes after the quota handoff warning",
            12,
        )
    ]
    assert found[0].id == "working-on-drain/engineer@abc-0001"


def test_a_blocked_codex_account_counts_as_draining():
    found = quota_view.findings(_drain_swarm("BLOCKED", harness="codex"), SLUG, Limits(), 1_000_000 + 10 * MINUTE)
    assert [f.summary for f in found] == [
        "still working on codex account alpha 10 minutes after its quota handoff warning"
    ]


@pytest.mark.parametrize(
    "kwargs,now",
    [
        ({}, 1_000_000 + 10 * MINUTE - 1),
        ({"state": "DRAIN_SOON"}, 1_000_000 + 60 * MINUTE),
        ({"state": "NORMAL"}, 1_000_000 + 60 * MINUTE),
        ({"agent_state": "finished"}, 1_000_000 + 60 * MINUTE),
        ({"idle_ticks": 1}, 1_000_000 + 60 * MINUTE),
    ],
)
def test_no_finding_before_the_grace_off_a_draining_account_or_when_the_agent_stopped(kwargs, now):
    assert quota_view.findings(_drain_swarm(**kwargs), SLUG, Limits(), now) == []


def test_no_finding_without_a_warning_or_with_a_warning_item_gone():
    store = _drain_swarm()
    store.redis.hset(store.key(SLUG, "quota-warnings"), "engineer@abc-0001", "missing")
    assert quota_view.findings(store, SLUG, Limits(), 1_000_000 + 60 * MINUTE) == []
    store.redis.delete(store.key(SLUG, "quota-warnings"))
    assert quota_view.findings(store, SLUG, Limits(), 1_000_000 + 60 * MINUTE) == []


def test_the_grace_follows_its_health_setting():
    assert Limits().drain_minutes == 10
    assert limits({"AGENTIHOOKS_HEALTH_DRAIN_MINUTES": "25"}).drain_minutes == 25
    store = _drain_swarm()
    assert quota_view.findings(store, SLUG, Limits(drain_minutes=25), 1_000_000 + 24 * MINUTE) == []
    assert len(quota_view.findings(store, SLUG, Limits(drain_minutes=25), 1_000_000 + 25 * MINUTE)) == 1


def test_swarm_status_prints_the_capacity_decision_and_the_drain_finding(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    from scripts.swarm import cli, status

    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    _drain_swarm(store=store, slug="sw")
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@abc-0001", title="Fold the chat panel")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000_000 + 12 * MINUTE)
    monkeypatch.setattr(status, "now_ms", lambda: 1_000_000 + 12 * MINUTE)
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    capsys.readouterr()
    run("sw", "status")
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("quota account claude alpha  drain  routing 7.5% left  sessions 1") - 1] == (
        "quota capacity eng 2 of 4, ci 1 of 1, plan 0 of 1, changed 12 minutes ago, because accounts are drain; "
        "Claude has 1 free seats and Codex has 0 free seats"
    )
    assert out[-5:] == [
        "finding  working on drain  engineer@abc-0001: still working on claude account alpha 12 minutes after its "
        "quota handoff warning",
        "  - account alpha is drain, routing 7.5% left",
        "  - task t1",
        "  threshold 10 minutes after the quota handoff warning",
        "  id working-on-drain/engineer@abc-0001",
    ]
