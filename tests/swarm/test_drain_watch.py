import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import drain_watch
from scripts.swarm.health.findings import Finding, Limits, limits
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_quota_view import DECISION, MINUTE, row

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "s"


def _store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def _drain_swarm(
    state="CLOSED",
    agent_state="working",
    idle_ticks=0,
    warned_at=1_000_000,
    harness="claude",
    store=None,
    slug=SLUG,
    week=7.5,
    started_at=0,
):
    store = store or _store()
    store.redis.set(
        store.key(slug, "quota-capacity"),
        json.dumps({**DECISION, "accounts": [row("alpha", state, 1, 40.0, week, harness)]}),
    )
    store.put_agent(
        slug,
        AgentRecord(
            "engineer@abc-0001",
            "eng",
            "t1",
            harness=harness,
            account="alpha",
            state=agent_state,
            idle_ticks=idle_ticks,
            started_at=started_at,
        ),
    )
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", "engineer@abc-0001", "QUOTA HANDOFF WARNING")
    store.redis.hset(inbox.key("item", item.id), "created_at", warned_at)
    store.redis.hset(store.key(slug, "quota-warnings"), "engineer@abc-0001", item.id)
    return store


def test_an_agent_still_working_on_a_closed_account_past_its_warning_is_a_finding():
    found = drain_watch.findings(_drain_swarm(), SLUG, Limits(), 1_000_000 + 12 * MINUTE)
    assert found == [
        Finding(
            "working on drain",
            "engineer@abc-0001",
            "still working on claude account alpha 12 minutes after its quota handoff warning",
            ("account alpha is closed, routing 7.5% left", "task t1"),
            "over 10 minutes after the quota handoff warning",
            12,
        )
    ]
    assert found[0].id == "working-on-drain/engineer@abc-0001"


def test_an_open_account_with_ten_percent_or_less_routing_left_counts_as_draining():
    assert drain_watch.draining(row("a", "OPEN", five=40.0, week=10.0), Limits())
    assert not drain_watch.draining(row("a", "OPEN", five=40.0, week=10.5), Limits())
    assert not drain_watch.draining(row("a", "UNKNOWN", five=None, week=3.0), Limits())
    assert drain_watch.draining(row("a", "OPEN", five=40.0, week=15.0), Limits(drain_left=15))
    assert limits({"AGENTIHOOKS_HEALTH_DRAIN_LEFT": "15"}).drain_left == 15
    found = drain_watch.findings(_drain_swarm("OPEN", week=9.0), SLUG, Limits(), 1_000_000 + 11 * MINUTE)
    assert [f.evidence for f in found] == [("account alpha is open, routing 9% left", "task t1")]


def test_a_closed_codex_account_counts_as_draining():
    found = drain_watch.findings(_drain_swarm(harness="codex"), SLUG, Limits(), 1_000_000 + 11 * MINUTE)
    assert [f.summary for f in found] == [
        "still working on codex account alpha 11 minutes after its quota handoff warning"
    ]


@pytest.mark.parametrize(
    "kwargs,now",
    [
        ({}, 1_000_000 + 11 * MINUTE - 1),
        ({"state": "OPEN", "week": 10.5}, 1_000_000 + 60 * MINUTE),
        ({"state": "UNKNOWN", "week": None}, 1_000_000 + 60 * MINUTE),
        ({"started_at": 1_000_001}, 1_000_000 + 60 * MINUTE),
        ({"agent_state": "finished"}, 1_000_000 + 60 * MINUTE),
        ({"idle_ticks": 1}, 1_000_000 + 60 * MINUTE),
    ],
)
def test_no_finding_until_past_the_grace_off_a_closed_account_or_when_the_agent_stopped(kwargs, now):
    assert drain_watch.findings(_drain_swarm(**kwargs), SLUG, Limits(), now) == []


def test_no_finding_without_a_warning_or_with_a_warning_item_gone():
    store = _drain_swarm()
    store.redis.hset(store.key(SLUG, "quota-warnings"), "engineer@abc-0001", "missing")
    assert drain_watch.findings(store, SLUG, Limits(), 1_000_000 + 60 * MINUTE) == []
    store.redis.delete(store.key(SLUG, "quota-warnings"))
    assert drain_watch.findings(store, SLUG, Limits(), 1_000_000 + 60 * MINUTE) == []


def test_no_capacity_record_means_no_finding():
    store = _drain_swarm()
    store.redis.delete(store.key(SLUG, "quota-capacity"))
    assert drain_watch.findings(store, SLUG, Limits(), 1_000_000 + 60 * MINUTE) == []


def test_a_warning_sent_at_the_agent_start_counts():
    found = drain_watch.findings(_drain_swarm(started_at=1_000_000), SLUG, Limits(), 1_000_000 + 11 * MINUTE)
    assert [f.subject for f in found] == ["engineer@abc-0001"]


@pytest.mark.parametrize("skip", ["idle", "open account", "warning gone"])
def test_an_agent_skipped_first_does_not_hide_a_later_one(skip):
    store = _drain_swarm()
    decision = json.loads(store.redis.get(store.key(SLUG, "quota-capacity")))
    decision["accounts"].append(row("beta", "OPEN", 1, 80.0, 60.0))
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(decision))
    first = AgentRecord(
        "engineer@aaa-0000",
        "eng",
        "t0",
        harness="claude",
        account="beta" if skip == "open account" else "alpha",
        state="working",
        idle_ticks=1 if skip == "idle" else 0,
    )
    agents = store.redis.hgetall(store.key(SLUG, "agents"))
    store.redis.delete(store.key(SLUG, "agents"))
    store.put_agent(SLUG, first)
    for name, value in agents.items():
        store.redis.hset(store.key(SLUG, "agents"), name, value)
    store.redis.hset(store.key(SLUG, "quota-warnings"), first.name, "missing")
    if skip != "warning gone":
        item = InboxStore(store.redis).send("swarm", first.name, "QUOTA HANDOFF WARNING")
        store.redis.hset(store.key(SLUG, "quota-warnings"), first.name, item.id)
    found = drain_watch.findings(store, SLUG, Limits(), 1_000_000 + 12 * MINUTE)
    assert [f.subject for f in found] == ["engineer@abc-0001"]


def test_the_grace_follows_its_health_setting():
    assert Limits().drain_minutes == 10
    assert limits({"AGENTIHOOKS_HEALTH_DRAIN_MINUTES": "25"}).drain_minutes == 25
    store = _drain_swarm()
    assert drain_watch.findings(store, SLUG, Limits(drain_minutes=25), 1_000_000 + 25 * MINUTE) == []
    assert len(drain_watch.findings(store, SLUG, Limits(drain_minutes=25), 1_000_000 + 26 * MINUTE)) == 1


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
    assert out[out.index("quota account claude alpha  closed  routing 7.5% left  sessions 1") - 1] == (
        "quota capacity eng 2 of 4, ci 1 of 1, plan 0 of 1, changed 12 minutes ago, because accounts are closed; "
        "Claude has 1 free seats and Codex has 0 free seats"
    )
    assert out[-5:] == [
        "finding  working on drain  engineer@abc-0001: still working on claude account alpha 12 minutes after its "
        "quota handoff warning",
        "  - account alpha is closed, routing 7.5% left",
        "  - task t1",
        "  threshold over 10 minutes after the quota handoff warning",
        "  id working-on-drain/engineer@abc-0001",
    ]
