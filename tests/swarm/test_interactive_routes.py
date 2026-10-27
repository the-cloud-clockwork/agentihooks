import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import Placed, placed_record
from tests.swarm.test_runtime import _passed, _resuming, validated


@pytest.fixture(autouse=True)
def native_status(monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setattr(native_login, "check", lambda *_: {"loggedIn": True, "authMethod": "claude.ai"})


@pytest.mark.parametrize("kind", ["subscription", "interactive", "api"])
def test_route_kind_persists_and_legacy_records_default_to_subscription(kind):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord("agent", "master", "master", route_kind=kind)
    store.put_agent("proof", agent)
    assert store.agents("proof")[0].route_kind == kind
    raw = json.loads(store.redis.hget(store.key("proof", "agents"), "agent"))
    del raw["route_kind"]
    store.redis.hset(store.key("proof", "agents"), "agent", json.dumps(raw))
    assert store.agents("proof")[0].route_kind == "subscription"


def test_placement_copies_interactive_route_kind_to_the_persisted_record():
    agent = placed_record(AgentRecord("agent", "master", "master"), Placed("pane", "claude", route_kind="interactive"))
    assert agent.route_kind == "interactive"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_resume_preserves_interactive_route_when_the_account_also_has_a_token(tmp_path, harness):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    agent = replace(agent, route_kind="interactive")
    placed = runtime.resume(config, agent, "resume")
    passed = _passed(seen["runs"][0])
    assert passed[passed.index("--route") + 1] == "interactive"
    assert placed.route_kind == "interactive"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_spawn_preserves_explicit_interactive_route_kind(tmp_path, harness):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": harness}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "master", "agent", {"id": "master", "title": "proof", "route_kind": "interactive"})
    passed = _passed(seen[0])
    assert passed[passed.index("--route") + 1] == "interactive"
    assert placed.route_kind == "interactive"


@pytest.mark.parametrize("account,kind", [("api", "api"), ("slug", "subscription")])
def test_spawn_records_the_api_route_kind_from_the_launch_report(tmp_path, account, kind):
    def run(argv, **kwargs):
        output = f"status=started\nroute_status=routed\naccount={account}\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, output), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "master", "agent", {"id": "master", "title": "proof", "route_kind": "subscription"})
    assert placed.account == account
    assert placed.route_kind == kind


@pytest.mark.parametrize("has_quota", [False, True])
@pytest.mark.parametrize("kind", ["interactive", "subscription", "api"])
def test_only_interactive_spawn_skips_subscription_quota(tmp_path, has_quota, kind):
    from scripts import agent_choice

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", agent_choice.ALL_FULL))
    if has_quota:
        runtime._quota_accounts = []
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof", "route_kind": kind}
    if kind != "interactive":
        from scripts.swarm.tick import SpawnError

        with pytest.raises(SpawnError):
            runtime.spawn(config, "master", "agent", task)
        assert not seen
        return
    placed = runtime.spawn(config, "master", "agent", task)
    assert placed.route_kind == "interactive"
    assert _passed(seen[0])[:2] == ["--route", "interactive"]


@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("week_left,route", [(3.0, "open"), (60.0, "interactive")])
def test_a_master_successor_leaves_the_spent_reserved_account_for_an_open_one(
    tmp_path, monkeypatch, week_left, route, handoff
):
    from scripts.routing import master_account
    from scripts.swarm.capacity import Account

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    declared = {"claude": master_account.MasterAccount("claude", "main")}
    monkeypatch.setattr(master_account, "load", lambda environ: declared)
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [
        Account("claude", "main", "OPEN", 0, 90.0, week_left, 6),
        Account("claude", "open", "OPEN", 0, 90.0, 60.0, 6),
    ]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof"}
    if handoff:
        task["handoff"] = "Continue the master task after the quota handoff."
        task["handoff_envelope"] = {
            "reason": "quota",
            "launch": {"profile": "master", "harness": "claude", "account": "main", "model": "opus", "effort": "high"},
        }
    placed = runtime.spawn(config, "master", "agent", task)
    passed = _passed(seen[0])
    assert passed[passed.index("--route") + 1] == route
    assert placed.route_kind == ("interactive" if route == "interactive" else "subscription")


@pytest.mark.parametrize("handoff", [False, True])
def test_a_front_door_master_successor_leaves_the_closed_reserved_login_account_for_an_open_one(
    tmp_path, monkeypatch, handoff
):
    from scripts import master_claude_login
    from scripts.routing import master_account
    from scripts.swarm.capacity import Account

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    monkeypatch.setattr(master_claude_login, "enabled", lambda *_: True)
    declared = {"claude": master_account.MasterAccount("claude", "main")}
    monkeypatch.setattr(master_account, "load", lambda environ: declared)
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [
        Account("claude", "main", "OPEN", 0, 96.0, 5.0, 6, reserved="workers", unreserved_cap=6),
        Account("claude", "open", "OPEN", 0, 95.0, 78.0, 6),
    ]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof", "seat": "master@proof"}
    if handoff:
        task["handoff"] = "Continue the master task after the quota handoff."
        task["handoff_envelope"] = {
            "reason": "quota",
            "launch": {"profile": "master", "harness": "claude", "account": "main", "model": "opus", "effort": "high"},
        }
    placed = runtime.spawn(config, "master", "agent", task)
    passed = _passed(seen[0])
    assert passed[passed.index("--route") + 1] == "open"
    assert placed.route_kind == "subscription"


@pytest.mark.parametrize(
    "rows,spent",
    [
        ([("main", 5.0, 6, "", None, "subscription"), ("open", 60.0, 6, "", None, "subscription")], True),
        ([("main", 5.5, 6, "", None, "subscription"), ("open", 60.0, 6, "", None, "subscription")], True),
        ([("main", 10.5, 6, "", None, "subscription"), ("open", 60.0, 6, "", None, "subscription")], False),
        ([("main", 3.0, 6, "", None, "subscription"), ("api", None, 4, "", None, "api")], False),
        ([("main", 3.0, 6, "", None, "subscription"), ("open", 60.0, 0, "workers", 6, "subscription")], True),
        ([("main", 3.0, 6, "", None, "subscription")], False),
        ([("open", 60.0, 6, "", None, "subscription")], False),
    ],
)
def test_the_declared_master_account_is_spent_at_the_handoff_warning_with_another_open_account(
    tmp_path, monkeypatch, rows, spent
):
    from scripts.routing import master_account
    from scripts.swarm.capacity import Account

    monkeypatch.setattr(
        master_account, "load", lambda environ: {"claude": master_account.MasterAccount("claude", "main")}
    )
    runtime = HerdrRuntime(home=tmp_path, run=lambda *a, **k: None, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [
        Account("claude", name, "OPEN", 0, 90.0, week, cap, kind=kind, reserved=reserved, unreserved_cap=unreserved)
        for name, week, cap, reserved, unreserved, kind in rows
    ]
    assert runtime._master_account_spent() is spent


def test_an_unreadable_master_account_keeps_the_interactive_route(tmp_path, monkeypatch):
    from redis.exceptions import RedisError

    from scripts.routing import master_account
    from scripts.swarm.capacity import Account

    def load(environ):
        raise RedisError("down")

    monkeypatch.setattr(master_account, "load", load)
    runtime = HerdrRuntime(home=tmp_path, run=lambda *a, **k: None, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [
        Account("claude", "main", "OPEN", 0, 90.0, 3.0, 6),
        Account("claude", "open", "OPEN", 0, 90.0, 60.0, 6),
    ]
    assert runtime._master_account_spent() is False
