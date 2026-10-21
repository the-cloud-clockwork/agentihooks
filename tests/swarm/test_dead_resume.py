from dataclasses import replace
from types import SimpleNamespace

import pytest

from hooks.context import context_recycle
from scripts.swarm import tick as tick_module
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed
from tests.swarm.profile_fixture import validated
from tests.swarm.test_runtime import _resuming
from tests.swarm.test_tick import FakeRuntime, tasks


@pytest.fixture
def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, compact_limit=600))
    return result


class ResumingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.resumed = []

    def resume(self, config, agent, text):
        assert config.slug == "sw"
        assert text == (
            "Your pane ended unexpectedly. Continue your task in this conversation and seat. "
            "Read your task progress and proof and handle your inbox before continuing."
        )
        self.resumed.append(agent)
        self.live.add(agent.name)
        self.conversation_ids["reopened:pane"] = agent.conversation_id
        return Placed("reopened:pane", agent.harness, profile=agent.profile)


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_dead_agent_with_room_resumes_its_conversation_and_seat(store, monkeypatch, lane):
    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    original = next(agent for agent in store.agents("sw") if agent.lane == lane)
    original = replace(original, conversation_id="original-conversation", profile="engineer", idle_ticks=4)
    store.put_agent("sw", original)
    runtime.live.discard(original.name)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    runtime.conversation_ids = {}

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert runtime.resumed == [original]
    reopened = next(agent for agent in store.agents("sw") if agent.name == original.name)
    assert reopened.conversation_id == original.conversation_id
    assert reopened.seat == original.seat
    assert reopened.generation == original.generation
    assert reopened.task == original.task
    assert reopened.pane_id == "reopened:pane"
    assert reopened.started_at == original.started_at
    assert reopened.launched_at == STARTUP_GRACE_MS + 2
    assert reopened.idle_ticks == 0
    assert (reopened.account, reopened.model, reopened.effort) == (original.account, original.model, original.effort)
    assert ledger.notes[-1] == f"Resumed {original.name} in its own conversation and seat after its pane ended."
    if lane == "eng":
        assert ledger.rows["t1"]["claimed_by"] == original.name
        assert store.claimant("sw", "t1") == original.name


def test_missing_pane_does_not_erase_a_known_conversation(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    original = next(agent for agent in store.agents("sw") if agent.lane == "master")
    store.put_agent("sw", replace(original, conversation_id="known-conversation"))
    runtime.conversation_ids = {}

    tick_module.tick("sw", store, ledger, runtime, 2)

    current = next(agent for agent in store.agents("sw") if agent.name == original.name)
    assert current.conversation_id == "known-conversation"


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_launch_persists_the_native_conversation(tmp_path, monkeypatch, lane):
    output = "status=started\nroute_status=routed\npane_id=original:pane\n"
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout=validated(argv, output), stderr=""),
        choose=lambda *_: ("claude", "open"),
    )
    monkeypatch.setattr(runtime, "conversations", lambda: {"original:pane": "native-conversation"})
    from scripts.swarm.store import AgentRecord, SwarmConfig

    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, code="a1b2c3")
    placed = runtime.spawn(config, lane, "agent@a1b2c3-0001", {"id": lane, "title": "Recovery"})
    record = tick_module.placed_record(AgentRecord("agent@a1b2c3-0001", lane, lane), placed)
    assert record.conversation_id == "native-conversation"


def test_resume_placement_carries_the_original_conversation(tmp_path):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    placed = runtime.resume(config, agent, "Continue the task")
    record = tick_module.placed_record(agent, placed)
    assert record.conversation_id == "c0ffee"
    assert placed.conversation_id == "c0ffee"


@pytest.mark.parametrize("lane", ["master", "eng"])
@pytest.mark.parametrize("reason", ["handoff", "limit", "failed"])
def test_dead_resume_records_why_a_fresh_agent_is_needed(store, monkeypatch, lane, reason):
    from scripts.swarm import dead_resume
    from scripts.swarm.tick import SpawnError

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == lane)
    agent = replace(agent, conversation_id="old-conversation", profile="engineer")
    store.put_agent("sw", agent)
    runtime.live.discard(agent.name)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 600_000 if reason == "limit" else 240_000)
    if reason == "handoff":
        store.put_handoff("sw", agent.task, "Submitted handoff")
    if reason == "failed":

        def fail(config, original, text):
            assert original == agent
            raise SpawnError("native resume refused")

        monkeypatch.setattr(runtime, "resume", fail)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10) is False
    explanations = {
        "handoff": "a handoff was submitted",
        "limit": "the conversation reached its compact limit",
        "failed": "resume failed: native resume refused",
    }
    assert ledger.notes[-1] == f"Starting a fresh agent after {agent.name} ended because {explanations[reason]}."
    assert runtime.resumed == []


@pytest.mark.parametrize("lane", ["master", "eng"])
def test_confirmed_dead_process_reaches_resume_before_binding_retirement(store, monkeypatch, lane):
    from scripts.swarm.live_binding import assignment

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == lane)
    agent = replace(agent, conversation_id="old-conversation", profile="engineer")
    store.put_agent("sw", agent)
    runtime.live.discard(agent.name)

    def bindings(agents):
        return {
            a.name: {**assignment(a), "hooks": True} if a.name in runtime.live else {"process": False} for a in agents
        }

    monkeypatch.setattr(runtime, "bindings", bindings)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert runtime.resumed == [agent]
    assert agent.pane_id not in runtime.closed


def test_lost_agent_cannot_reopen_a_successors_claim(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    agent = replace(agent, conversation_id="old-conversation", profile="engineer")
    store.put_agent("sw", agent)
    rows = {"t1": dict(ledger.rows["t1"])}
    original_resume = runtime.resume

    def resume_and_replace(config, original, text):
        placed = original_resume(config, original, text)
        ledger.rows["t1"].update(claimed_by="successor", claim_generation=99, state="pr")
        return placed

    monkeypatch.setattr(runtime, "resume", resume_and_replace)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    tick_module._lost("sw", store, ledger, runtime, rows, agent, 10)

    assert ledger.rows["t1"]["claimed_by"] == "successor"
    assert ledger.rows["t1"]["claim_generation"] == 99
    assert ledger.rows["t1"]["state"] == "pr"
    assert "reopened:pane" in runtime.closed
    assert all(current.name != agent.name for current in store.agents("sw"))


def test_resumed_agent_keeps_the_runtime_model_and_route(store, monkeypatch):
    from scripts.swarm import dead_resume

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == "master")
    agent = store.start_execution(
        "sw",
        replace(
            agent,
            conversation_id="old-conversation",
            profile="engineer",
            idle_ticks=4,
            profile_decision={"retained": True, "shared": "old"},
        ),
    )
    store.put_agent("sw", agent)
    original_resume = runtime.resume
    remote = {"enabled": True, "name": "master"}
    timings = {"launched_at": 8, "returned_at": 9}

    def resume_on_login(config, original, text):
        return replace(
            original_resume(config, original, text),
            account="new-account",
            profile="restored-profile",
            model="resolved-model",
            effort="high",
            model_source="recorded",
            route_kind="interactive",
            remote_control=remote,
            desired_route_kind="interactive",
            profile_decision={"reported": True, "shared": "new"},
            launch_timings=timings,
            launched_at=8,
        )

    def used(session):
        assert session == "old-conversation"
        return 240_000

    monkeypatch.setattr(runtime, "resume", resume_on_login)
    monkeypatch.setattr(context_recycle, "_used", used)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10)
    current = next(a for a in store.agents("sw") if a.name == agent.name)
    assert (current.model, current.effort, current.model_source) == ("resolved-model", "high", "recorded")
    assert current.route_kind == current.desired_route_kind == "interactive"
    assert current.remote_control == remote
    assert current.conversation_id == "old-conversation"
    assert current.account == "new-account"
    assert current.profile == "restored-profile"
    assert current.profile_decision == {"retained": True, "reported": True, "shared": "new"}
    assert current.launch_timings == timings
    assert current.started_at == agent.started_at
    assert current.launched_at == 8
    assert current.idle_ticks == 0
    assert store.names.entry(agent.name)["session_id"] == "old-conversation"


def test_dead_master_recovers_after_a_peers_binding_mismatch(store, monkeypatch):
    from scripts.swarm.live_binding import assignment

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    master = next(agent for agent in store.agents("sw") if agent.lane == "master")
    master = replace(master, conversation_id="master-conversation", profile="engineer")
    store.put_agent("sw", master)
    runtime.live.discard(master.name)

    def bindings(agents):
        return {
            agent.name: {**assignment(agent), "hooks": False} if agent.name in runtime.live else {"process": False}
            for agent in agents
        }

    monkeypatch.setattr(runtime, "bindings", bindings)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert runtime.resumed == [master]
    assert "engineer@a1b2c3-0001" in runtime.killed


def prepared_recovery(store):
    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(agent for agent in store.agents("sw") if agent.lane == "eng")
    agent = replace(agent, conversation_id="original-conversation", profile="engineer")
    store.put_agent("sw", agent)
    runtime.live.discard(agent.name)
    return ledger, runtime, agent


def test_recorded_legacy_recovery_keeps_telemetry_attributed(store, monkeypatch):
    from dataclasses import asdict

    from scripts.doctor import registry, traces
    from scripts.swarm import dead_resume
    from scripts.swarm.store import AgentRecord

    task = "doc-c688728d8ef70155"
    ledger, runtime = tasks((task, "eng")), ResumingRuntime()
    agent = AgentRecord(
        name="engineer@323133-2021",
        lane="eng",
        task=task,
        pane_id="w4V:pEC",
        harness="claude",
        started_at=1792395207377,
        launched_at=1792395275330,
        seat="eng-1@rig-grade-swarm",
        conversation_id="9a2f0f72-e3c7-4613-9dde-637079237333",
        profile="documentation",
    )
    ledger.rows[task].update(state="claimed", claimed_by=agent.name)
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, agent.name, agent.started_at)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 1792397325121)

    [recovered] = store.agents("sw")
    [binding] = registry.bindings([asdict(recovered)])
    binding.update(
        read=True,
        local=None,
        remote={"observations": 1},
        traces=[
            {
                "id": "ea8dca04fa7bb6b6ff79e6b6fb8152a4",
                "session_id": "9a2f0f72-e3c7-4613-9dde-637079237333",
                "life": "engineer@323133-2021#1792395207377",
                "seat": "eng-1@rig-grade-swarm",
                "task": "doc-c688728d8ef70155",
                "harness": "claude",
                "profile": "documentation",
            }
        ],
    )
    record = {"now_ms": 1792398922072, "active": [binding]}
    assert binding["life"] == "engineer@323133-2021#1792395207377"
    assert traces.active(record, traces.Limits()) == []
    binding.update(life="engineer@323133-2021#1792397325121", started_at=1792397325121)
    [finding] = traces.active(record, traces.Limits())
    assert finding.id == (
        "telemetry-misattributed/engineer@323133-2021.1792397325121.life=engineer@323133-2021#1792395207377"
    )
    assert finding.measure == 1


def test_a_fresh_conversation_starts_a_new_life(store):
    ledger, runtime, original = prepared_recovery(store)
    original = replace(original, conversation_id="")
    store.put_agent("sw", original)

    tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    fresh = next(agent for agent in store.agents("sw") if agent.lane == "eng")
    assert runtime.resumed == []
    assert fresh.name != original.name
    assert fresh.started_at == STARTUP_GRACE_MS + 2
    assert fresh.started_at != original.started_at


def test_dead_retiring_agent_retries_retirement(store, monkeypatch):
    from scripts.swarm.live_binding import assignment

    ledger, runtime, agent = prepared_recovery(store)
    agent = replace(agent, state="retiring")
    store.put_agent("sw", agent)

    def bindings(agents):
        return {
            a.name: {**assignment(a), "hooks": True} if a.name in runtime.live else {"process": False} for a in agents
        }

    monkeypatch.setattr(runtime, "bindings", bindings)

    actions = tick_module.tick("sw", store, ledger, runtime, STARTUP_GRACE_MS + 2)

    assert agent.pane_id in runtime.closed
    assert agent.name not in {row.name for row in store.agents("sw")}
    assert runtime.resumed == []
    assert any(action.startswith(f"retired {agent.name} after mismatched process") for action in actions)


@pytest.mark.parametrize("control", ["stopped", "stopping", "record", "seat", "owner", "state", "claim"])
def test_recovery_refuses_a_stopped_swarm_or_stale_ownership(store, monkeypatch, control):
    from scripts.swarm import dead_resume

    ledger, runtime, agent = prepared_recovery(store)
    if control in {"stopped", "stopping"}:
        store.update("sw", state=control)
    elif control == "record":
        store.drop_agent("sw", agent.name)
    elif control == "seat":
        store.seats.occupy(agent.seat, store.next_name("sw", "eng", 2), 2)
    elif control == "owner":
        ledger.rows["t1"]["claimed_by"] = "successor"
    elif control == "state":
        ledger.rows["t1"]["state"] = "done"
    else:
        store.release("sw", "t1", agent.name)
        assert store.claim("sw", "t1", "successor", 100_000)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    notes = list(ledger.notes)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10) is False
    assert runtime.resumed == []
    assert ledger.notes == notes


def test_dead_resume_keeps_a_pull_request_claim_and_checks_ledger_identity(store, monkeypatch):
    from scripts.swarm import dead_resume

    ledger, runtime, agent = prepared_recovery(store)
    ledger.rows["t1"]["state"] = "pr"
    original_task = ledger.task

    def task(slug, task_id):
        assert slug == "sw"
        assert task_id == "t1"
        return original_task(slug, task_id)

    monkeypatch.setattr(ledger, "task", task)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10)
    assert ledger.rows["t1"]["state"] == "pr"
    assert ledger.rows["t1"]["claimed_by"] == agent.name


@pytest.mark.parametrize("compact_limit,expected", [(0, "the conversation reached its compact limit"), (700, "")])
def test_recovery_uses_the_configured_compact_limit_or_default(store, monkeypatch, compact_limit, expected):
    from scripts.swarm import dead_resume

    ledger, runtime, agent = prepared_recovery(store)
    store.update("sw", compact_limit=compact_limit)
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 600)

    def used(session):
        assert session == "original-conversation"
        return 650_000

    monkeypatch.setattr(context_recycle, "_used", used)

    assert dead_resume._reason(store, "sw", agent) == expected


def test_missing_conversation_records_the_exact_resume_failure(store):
    from scripts.swarm import dead_resume

    ledger, runtime, agent = prepared_recovery(store)
    agent = replace(agent, conversation_id="")
    store.put_agent("sw", agent)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10) is False
    assert ledger.notes[-1] == (
        f"Starting a fresh agent after {agent.name} ended because "
        "resume failed because no conversation id was recorded."
    )


def test_resume_preserves_the_id_when_native_discovery_arrives_after_launch(tmp_path, monkeypatch):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    observations = iter([{}, {"w2:p9": "c0ffee"}])
    monkeypatch.setattr(runtime, "conversations", lambda: next(observations))

    placed = runtime.resume(config, agent, "Continue the task")

    assert placed.conversation_id == "c0ffee"


def test_recovery_renews_its_claim_for_the_tick_lease(store, monkeypatch):
    from scripts.swarm import dead_resume
    from scripts.swarm.tick import LEASE_MS

    ledger, runtime, agent = prepared_recovery(store)
    original_claim = store.ensure_claim

    def claim(slug, current, lease_ms):
        assert (slug, current, lease_ms) == ("sw", agent, LEASE_MS)
        return original_claim(slug, current, lease_ms)

    monkeypatch.setattr(store, "ensure_claim", claim)
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)

    assert dead_resume.reopen("sw", store, ledger, runtime, agent, 10)
    assert 0 < store.redis.pttl(store.key("sw", "claim", "t1")) <= LEASE_MS


@pytest.mark.parametrize(
    "state,fact,live,expected",
    [
        ("retiring", "missing", False, False),
        ("retiring", "dead", False, False),
        ("working", "missing", False, True),
        ("working", "reported", False, False),
        ("working", "dead", False, True),
        ("working", "dead", True, False),
    ],
)
def test_binding_deferral_keeps_retiring_and_reported_agents_in_verification(store, state, fact, live, expected):
    from scripts.swarm import dead_resume

    ledger, runtime, agent = prepared_recovery(store)
    agent = replace(agent, state=state)
    facts = {} if fact == "missing" else {agent.name: {"process": fact != "dead"}}
    names = {agent.name} if live else set()

    assert dead_resume.skip_binding(agent, facts, names) is expected


def test_a_legacy_lost_claim_without_a_generation_reopens(store):
    ledger, runtime, agent = prepared_recovery(store)
    ledger.rows["t1"].pop("claim_generation")
    rows = {"t1": dict(ledger.rows["t1"])}

    assert tick_module._drop("sw", store, ledger, rows, agent) == ", task t1 reopened"
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""


def test_reap_reports_every_lost_engineer_in_the_pass(store):
    store.update("sw", max_eng=2)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    engineers = [agent for agent in store.agents("sw") if agent.lane == "eng"]
    for agent in engineers:
        runtime.live.discard(agent.name)
    rows = {task_id: dict(row) for task_id, row in ledger.rows.items()}

    actions = tick_module._reap("sw", store, ledger, runtime, rows, STARTUP_GRACE_MS + 2)

    assert actions == [f"lost {agent.name}, task {agent.task} reopened" for agent in engineers]
    assert len(engineers) == 2


def test_a_deliberately_terminated_engineer_is_not_resumed_and_its_task_reopens(store, monkeypatch):
    from scripts.swarm import deliberate_exit

    ledger, runtime = tasks(("t1", "eng")), ResumingRuntime()
    tick_module.tick("sw", store, ledger, runtime, 1)
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    store.put_agent("sw", replace(agent, conversation_id="original-conversation", profile="engineer"))
    monkeypatch.setattr(context_recycle, "_used", lambda session: 240_000)
    monkeypatch.setattr(tick_module.reaper, "scratch_homes", lambda slug, task: [f"{slug}/{task}"])
    claims = store.claims("sw", "t1")
    deliberate_exit.record(store, "sw", agent.name, 5)
    runtime.live.discard(agent.name)
    rows = {task_id: dict(row) for task_id, row in ledger.rows.items()}

    actions = tick_module._reap("sw", store, ledger, runtime, rows, STARTUP_GRACE_MS + 2)

    assert actions == [f"retired {agent.name} after a deliberate terminate, task t1 reopened"]
    assert runtime.homes[agent.name] == ["sw/t1"]
    assert runtime.resumed == []
    assert agent.name not in {a.name for a in store.agents("sw")}
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert store.claims("sw", "t1") == claims - 1
    assert deliberate_exit.recorded(store, "sw", agent.name) is False


def test_a_deliberate_exit_overrides_a_pending_outage_recovery(store):
    import json

    from scripts.swarm import deliberate_exit, resume

    ledger, runtime, agent = prepared_recovery(store)
    identity = resume._recovery_identity(store, "sw", agent)
    record = {"state": "pending", "attempts": 0, "identity": identity, "last_at": 0}
    store.redis.hset(store.key("sw", resume.RECOVERY), agent.name, json.dumps(record))
    assert resume.recovery_pending(store, "sw", agent) is True
    deliberate_exit.record(store, "sw", agent.name, 5)
    rows = {task_id: dict(row) for task_id, row in ledger.rows.items()}

    assert resume.recovery_pending(store, "sw", agent) is False
    assert resume.recover_outages("sw", store, ledger, runtime, rows, STARTUP_GRACE_MS + 2) == []
    actions = tick_module._reap("sw", store, ledger, runtime, rows, STARTUP_GRACE_MS + 2)

    assert actions == [f"retired {agent.name} after a deliberate terminate, task t1 reopened"]
    assert runtime.resumed == []
    assert ledger.rows["t1"]["state"] == "open"


def test_a_deliberate_exit_is_kept_per_swarm_and_agent_until_taken(store):
    from scripts.swarm import deliberate_exit

    deliberate_exit.record(store, "sw", "eng@a-1", 7)

    assert deliberate_exit.recorded(store, "sw", "eng@a-1") is True
    assert deliberate_exit.recorded(store, "sw", "eng@a-2") is False
    assert deliberate_exit.recorded(store, "other", "eng@a-1") is False
    assert store.redis.hget(store.key("sw", "deliberate-exits"), "eng@a-1") == "7"
    assert deliberate_exit.take(store, "sw", "eng@a-1") is True
    assert deliberate_exit.take(store, "sw", "eng@a-1") is False
    assert deliberate_exit.recorded(store, "sw", "eng@a-1") is False
