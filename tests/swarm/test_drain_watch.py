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
    state="CLOSED", agent_state="working", idle_ticks=0, warned_at=1_000_000, harness="claude", store=None, slug=SLUG
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


def test_a_closed_codex_account_counts_as_draining():
    found = drain_watch.findings(_drain_swarm(harness="codex"), SLUG, Limits(), 1_000_000 + 11 * MINUTE)
    assert [f.summary for f in found] == [
        "still working on codex account alpha 11 minutes after its quota handoff warning"
    ]


@pytest.mark.parametrize(
    "kwargs,now",
    [
        ({}, 1_000_000 + 11 * MINUTE - 1),
        ({"state": "OPEN"}, 1_000_000 + 60 * MINUTE),
        ({"state": "UNKNOWN"}, 1_000_000 + 60 * MINUTE),
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
