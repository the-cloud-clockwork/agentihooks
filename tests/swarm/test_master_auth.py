from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("quota_handoff", [False, True])
@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("five_left,week_left,cap", [(90, 25, 6), (90, 9, 6), (4, 25, 0), (90, 25, None), (90, 25, 0)])
def test_only_the_master_can_use_reserved_room(tmp_path, lane, five_left, week_left, cap, quota_handoff):
    from scripts.routing.reservation import Reservation
    from scripts.swarm.capacity import Account, reserve
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=reserved\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    state = "UNKNOWN" if cap is None else "OPEN" if cap else "CLOSED"
    runtime._quota_accounts = reserve(
        [Account("claude", "reserved", state, 0, five_left, week_left, cap)],
        Reservation("claude", "reserved", 1),
    )
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": lane, "title": "proof", "seat": "master-2@proof", "route_kind": "subscription"}
    if quota_handoff:
        task["handoff"] = "Continue the task after quota handoff."
        task["handoff_envelope"] = {
            "reason": "quota",
            "launch": {
                "profile": "master" if lane == "master" else "engineer",
                "harness": "claude",
                "account": "previous",
                "model": "opus",
                "effort": "high",
            },
        }
    if lane != "master" or five_left == 4 or week_left == 9 or not cap:
        with pytest.raises(SpawnError):
            runtime.spawn(config, lane, "agent", task)
        assert not seen
        return
    placed = runtime.spawn(config, lane, "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "reserved"]
    assert placed.account == "reserved"
    assert placed.route_kind == "subscription"
    assert runtime._quota_accounts[0].sessions == 1
    assert runtime._quota_accounts[0].cap == 0


@pytest.mark.parametrize("login", [True, False])
@pytest.mark.parametrize("handoff", [True, False])
def test_master_prefers_a_valid_login_even_after_a_token_handoff(tmp_path, login, handoff):
    from scripts.routing import native_login
    from scripts.swarm.capacity import Account
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=0 if login else 1, stdout=native_login.STATUS if login else "", stderr="")
        seen.append(argv)
        out = "status=started\nroute_status=routed\naccount=token\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [Account("claude", "token", "OPEN", 0, 90, 90, 6)]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    task = {"id": "master", "title": "proof", "seat": "master-2@proof"}
    if handoff:
        task["handoff"] = "Continue the master task."
        task["handoff_envelope"] = {
            "launch": {"profile": "master", "harness": "claude", "account": "token", "model": "opus", "effort": "high"}
        }
    placed = runtime.spawn(config, "master", "agent", task)
    passed = seen[0][seen[0].index("--") + 1 :]
    assert passed[:2] == ["--route", "interactive" if login else "token"]
    assert placed.route_kind == ("interactive" if login else "subscription")


@pytest.mark.parametrize("kind", ["subscription", "api", "interactive"])
@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("desired", ["interactive", ""])
@pytest.mark.parametrize("seat", ["master@proof", "master-2@proof", "master-3@proof"])
def test_health_reports_only_master_fallback_and_clears_after_move_back(kind, lane, desired, seat):
    from scripts.swarm.health.findings import Limits, findings

    agent = {
        "name": "agent",
        "seat": seat,
        "lane": lane,
        "task": lane,
        "state": "working",
        "route_kind": kind,
        "desired_route_kind": desired,
    }
    found = [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]
    if lane == "master" and seat == "master@proof" and desired == "interactive" and kind != "interactive":
        assert len(found) == 1
        assert found[0].id == "master-fallback-auth/master@proof"
        assert found[0].summary == "master on fallback auth, phone access lost"
        assert found[0].measure == 1
        assert f"route: {kind}" in found[0].evidence
    else:
        assert found == []


def test_finished_fallback_master_raises_no_finding_and_unseated_master_uses_its_name():
    from scripts.swarm.health.findings import Limits, findings

    agent = {
        "name": "master-agent",
        "lane": "master",
        "task": "master",
        "state": "working",
        "route_kind": "subscription",
        "desired_route_kind": "interactive",
    }
    found = [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]
    assert [f.id for f in found] == ["master-fallback-auth/master-agent"]
    agent["state"] = "finished"
    assert not [f for f in findings({}, [agent], {}, Limits()) if f.kind == "master-fallback-auth"]


def test_fallback_finding_reads_every_agent_and_names_an_unrecorded_route_as_subscription():
    from scripts.swarm.health.findings import Limits, findings

    worker = {"name": "worker", "lane": "eng", "task": "t1", "state": "working"}
    master = {
        "name": "master-agent",
        "seat": "master@proof",
        "lane": "master",
        "task": "master",
        "state": "working",
        "desired_route_kind": "interactive",
    }
    found = [f for f in findings({}, [worker, master], {}, Limits()) if f.kind == "master-fallback-auth"]
    assert [f.evidence for f in found] == [("route: subscription", "agent master-agent")]
    assert found[0].threshold == "the master desires interactive authentication but runs on fallback authentication"


def test_unreserved_rows_regain_the_state_of_their_original_cap():
    from scripts.routing.reservation import Reservation
    from scripts.swarm.capacity import Account, reserve, unreserved

    held = reserve([Account("claude", "reserved", "OPEN", 0, 90, 25, 6)], Reservation("claude", "reserved", 1))
    assert (held[0].cap, held[0].state) == (0, "CLOSED")
    row = unreserved(held)[0]
    assert (row.cap, row.state, row.reserved) == (6, "OPEN", "")
    drained = unreserved(reserve([Account("claude", "low", "DRAIN", 0, 8, 25, 6)], Reservation("claude", "low", 1)))
    assert drained[0].state == "DRAIN"
    codex = unreserved(reserve([Account("codex", "weekly", "UNKNOWN", 0, 5, 25, 2)], Reservation("codex", "weekly", 1)))
    assert codex[0].state == "OPEN"


def _master_spawn(tmp_path, login, task):
    from scripts.routing import native_login
    from scripts.swarm.capacity import Account
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=0 if login else 1, stdout=native_login.STATUS if login else "", stderr="")
        out = "status=started\nroute_status=routed\naccount=token\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime._quota_accounts = [Account("claude", "token", "OPEN", 0, 90, 90, 6)]
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    return runtime.spawn(config, "master", "agent", task)


def test_fallback_launch_raises_the_finding_and_a_valid_login_at_handoff_clears_it(tmp_path):
    from dataclasses import replace

    import fakeredis

    from scripts.swarm.health.findings import Limits, findings
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import placed_record

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))

    def stored(placed):
        store.put_agent("proof", placed_record(AgentRecord("agent", "master", "master", seat="master@proof"), placed))
        return [f.id for f in findings({}, [store.agents("proof")[0].__dict__], {}, Limits())]

    fallback = replace(_master_spawn(tmp_path, False, {"id": "master", "title": "proof"}), route_kind="subscription")
    assert (fallback.route_kind, fallback.desired_route_kind) == ("subscription", "interactive")
    assert "master-fallback-auth/master@proof" in stored(fallback)
    handoff = {
        "id": "master",
        "title": "proof",
        "handoff": "Continue the master task.",
        "handoff_envelope": {
            "launch": {"profile": "master", "harness": "claude", "account": "token", "model": "opus", "effort": "high"}
        },
    }
    moved = _master_spawn(tmp_path, True, handoff)
    assert (moved.route_kind, moved.desired_route_kind) == ("interactive", "interactive")
    assert "master-fallback-auth/master@proof" not in stored(moved)


def test_desired_route_survives_storage_outside_the_strict_projection():
    import json
    from dataclasses import replace

    import fakeredis

    from scripts.swarm.store import AgentRecord, RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    record = AgentRecord("agent", "master", "master", seat="master@proof", desired_route_kind="interactive")
    store.put_agent("proof", record)
    assert "desired_route_kind" not in json.loads(store.redis.hget(store.key("proof", "agents"), "agent"))
    assert store.agents("proof")[0].desired_route_kind == "interactive"
    store.put_agent("proof", replace(record, idle_ticks=2))
    assert store.agents("proof")[0].desired_route_kind == "interactive"
    store.put_agent("proof", replace(record, desired_route_kind=""))
    assert store.agents("proof")[0].desired_route_kind == ""
    store.put_agent("proof", record)
    store.drop_agent("proof", "agent")
    store.put_agent("proof", replace(record, desired_route_kind=""))
    assert store.agents("proof")[0].desired_route_kind == ""


@pytest.mark.parametrize(
    "lane,harness,route,desired",
    [
        ("master", "claude", None, "interactive"),
        ("master", "claude", "subscription", ""),
        ("master", "codex", None, ""),
        ("eng", "claude", None, None),
    ],
)
@pytest.mark.parametrize("seat", ["master@proof", "master-2@proof", "master-3@proof", ""])
def test_only_a_claude_master_without_an_ordered_route_desires_the_login(tmp_path, lane, harness, route, desired, seat):
    from scripts.routing import native_login
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        out = "status=started\nroute_status=routed\naccount=token\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": harness}},
        autonomy="delegate",
    )
    task = {"id": lane, "title": "proof", "seat": seat, **({"route_kind": route} if route else {})}
    expected = "" if lane == "master" and seat in ("master-2@proof", "master-3@proof") else desired
    assert runtime.spawn(config, lane, "agent", task).desired_route_kind == expected


@pytest.mark.parametrize("placed,kept", [("", ""), ("interactive", "interactive"), (None, "interactive")])
def test_a_placement_decides_the_desired_route_and_an_undecided_one_keeps_the_record(placed, kept):
    from scripts.swarm.store import AgentRecord
    from scripts.swarm.tick import Placed, placed_record

    record = AgentRecord("agent", "master", "master", desired_route_kind="interactive")
    decided = Placed("pane", "claude") if placed is None else Placed("pane", "claude", desired_route_kind=placed)
    assert placed_record(record, decided).desired_route_kind == kept


def _login_runtime(tmp_path, accounts=None):
    from scripts.routing import native_login
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    seen = []

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            return SimpleNamespace(returncode=0, stdout=native_login.STATUS, stderr="")
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", "requested"))
    if accounts is not None:
        runtime._quota_accounts = accounts
    config = SimpleNamespace(
        slug="proof", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={"master": {}}, autonomy="delegate"
    )
    return runtime, config, seen


@pytest.mark.parametrize("claude_only", [False, True])
def test_a_master_with_no_harness_set_takes_the_login_on_claude(tmp_path, monkeypatch, claude_only):
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda name: claude_only)
    runtime, config, _ = _login_runtime(tmp_path)
    task = {"id": "master", "title": "proof"}
    if claude_only:
        task["handoff"] = "Continue the master task."
        task["handoff_envelope"] = {
            "launch": {"profile": "master", "harness": "codex", "account": "", "model": "opus", "effort": "high"}
        }
    placed = runtime.spawn(config, "master", "agent", task)
    assert (placed.harness, placed.route_kind, placed.choice) == ("claude", "interactive", "other")


@pytest.mark.parametrize("ordered", ["", "interactive"])
def test_a_master_after_a_codex_predecessor_keeps_codex(tmp_path, monkeypatch, ordered):
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda name: False)
    runtime, config, _ = _login_runtime(tmp_path)
    task = {
        "id": "master",
        "title": "proof",
        "handoff": "Continue the master task.",
        "handoff_envelope": {
            "launch": {"profile": "master", "harness": "codex", "account": "", "model": "sol", "effort": "high"}
        },
        **({"route_kind": ordered} if ordered else {}),
    }
    placed = runtime.spawn(config, "master", "agent", task)
    assert (placed.harness, placed.route_kind) == ("codex", ordered or "subscription")


def test_the_login_probe_runs_as_the_native_interactive_account(tmp_path):
    from scripts.routing import envs, native_login
    from scripts.swarm.runtime import HerdrRuntime
    from tests.swarm.profile_fixture import validated

    probes = []

    def run(argv, **kwargs):
        if argv[1:] == list(native_login.COMMAND):
            probes.append(kwargs["env"])
            return SimpleNamespace(returncode=0, stdout=native_login.STATUS, stderr="")
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": "claude"}},
        autonomy="delegate",
    )
    assert (
        runtime.spawn(
            config, "master", "agent", {"id": "master", "title": "proof", "seat": "master-2@proof"}
        ).route_kind
        == "interactive"
    )
    assert probes and probes[0].get(f"{envs.INTERACTIVE_PREFIX}native") == "1"


def test_a_valid_login_skips_the_quota_transfer_of_a_master_handoff(tmp_path):
    from scripts.swarm.capacity import Account

    runtime, config, seen = _login_runtime(tmp_path, [Account("claude", "token", "CLOSED", 0, 1, 1, 0)])
    config.lanes = {"master": {"agent": "claude"}}
    task = {
        "id": "master",
        "title": "proof",
        "handoff": "Continue the master task.",
        "handoff_envelope": {
            "reason": "quota",
            "launch": {"profile": "master", "harness": "claude", "account": "token", "model": "opus", "effort": "high"},
        },
    }
    placed = runtime.spawn(config, "master", "agent", task)
    assert placed.route_kind == "interactive"
    assert seen[0][seen[0].index("--") + 1 :][:2] == ["--route", "claude-ai"]


def test_a_resumed_master_keeps_its_desired_route(tmp_path):
    from dataclasses import replace

    from tests.swarm.test_runtime import _resuming

    runtime, config, agent, _ = _resuming(tmp_path, "c0ffee")
    config.slug = "proof"
    master = replace(agent, lane="master", task="master", route_kind="subscription", desired_route_kind="interactive")
    assert runtime.resume(config, master, "resume").desired_route_kind == "interactive"
