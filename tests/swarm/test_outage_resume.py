import json
from dataclasses import replace

import pytest

from scripts.swarm import idle, outage, profile_choice, prompt, resume
from scripts.swarm.pane import PaneObservation
from scripts.swarm.tick import Placed, SpawnError, tick
from scripts.swarm_ledger import ledger_workspace
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit

MINUTE = 60_000


class RecoveringRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.errors, self.resumed, self.resume_configs = {}, [], []
        self.inputs, self.titles, self.placement, self.spawn_configs = {}, {}, {}, []

    def observe(self, agent):
        return PaneObservation(
            self.status(agent),
            prompt_title=self.titles.get(agent.name, ""),
            typed=self.inputs.get(agent.name, ""),
            provider_error=self.errors.get(agent.name, ""),
        )

    def spawn(self, config, lane, name, task):
        self.spawn_configs.append(config.slug)
        return super().spawn(config, lane, name, task)

    def nudge(self, agent, text):
        result = super().nudge(agent, text)
        if not result:
            self.statuses[agent.name] = "working"
            self.errors.pop(agent.name, None)
        return result

    def resume(self, config, agent, text):
        self.resumed.append((agent.name, agent.conversation_id, text))
        self.resume_configs.append(config)
        self.live.add(agent.name)
        self.statuses[agent.name] = "working"
        return Placed("recovered:pane", agent.harness, model="sonnet", profile=agent.profile, **self.placement)


@pytest.fixture(autouse=True)
def outage_home(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)


def three_owners(store):
    store.update("sw", max_eng=3)
    ledger = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng"))
    runtime = RecoveringRuntime()
    tick("sw", store, ledger, runtime, 1_000)
    store.update("sw", state="paused")
    agents = [agent for agent in store.agents("sw") if agent.lane == "eng"]
    for index, agent in enumerate(agents):
        store.put_agent("sw", replace(agent, conversation_id=f"conversation-{index}", profile="engineer"))
        runtime.statuses[agent.name] = "idle"
    return ledger, runtime, agents


@pytest.mark.parametrize("error_state", ["idle", "waiting"])
def test_error_idle_and_dead_owners_resume_their_tasks_within_three_recovery_ticks(
    store, tmp_path, monkeypatch, error_state
):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    claims = {agent.task: store.claimant("sw", agent.task) for agent in agents}
    launches = list(runtime.spawned)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.errors[agents[0].name] = "provider unavailable"
    runtime.statuses[agents[0].name] = error_state
    runtime.live.remove(agents[2].name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    assert {agent.task: store.claimant("sw", agent.task) for agent in agents} == claims
    outage.observe("sw", "provider", [], 9 * MINUTE)
    for step in range(3):
        tick("sw", store, ledger, runtime, (9 + step) * MINUTE)
    assert all(runtime.statuses[agent.name] == "working" for agent in agents), (
        runtime.statuses,
        runtime.resumed,
        runtime.spawned,
    )
    assert {agent.task: store.claimant("sw", agent.task) for agent in agents} == claims
    assert runtime.spawned == launches
    assert runtime.nudged == [agents[0].name, agents[1].name]
    assert runtime.resumed[0][:2] == (agents[2].name, "conversation-2")
    for _, text in runtime.nudge_calls:
        assert "Service is back" in text
        assert "steering.md" in text and "progress.md" in text and "proof.md" in text
        assert "agentihooks msg inbox" in text
        assert "continue your own task" in text


def test_a_codex_owner_stopped_at_model_capacity_is_held_probed_then_resumed(store, tmp_path, monkeypatch):
    from scripts.swarm import pane, provider_probe, snapshot

    ledger, runtime, agents = three_owners(store)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    probes = []
    monkeypatch.setattr(
        provider_probe, "check", lambda target, *args: probes.append(target) or provider_probe.Result(True, "ok")
    )
    owner = replace(agents[0], conversation_id="conversation-0", profile="engineer", harness="codex", account="alpha")
    store.put_agent("sw", owner)
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    capacity = "■ Selected model is at capacity. Please try a different model."
    runtime.errors[owner.name] = pane.provider_error("codex", capacity)
    outage.providers(store, "sw", runtime, MINUTE)
    assert outage.open_records("sw")
    for step in range(3):
        tick("sw", store, ledger, runtime, (2 + step) * MINUTE)
    assert outage.held(store, "sw", owner.name)
    assert store.claimant("sw", owner.task) == owner.name
    assert owner.name not in runtime.nudged
    outage.providers(store, "sw", runtime, 5 * MINUTE)
    assert [(target.harness, target.account) for target in probes] == [("codex", "alpha")]
    assert not outage.open_records("sw")
    for step in range(3):
        tick("sw", store, ledger, runtime, (6 + step) * MINUTE)
    assert runtime.statuses[owner.name] == "working"
    assert store.claimant("sw", owner.task) == owner.name
    owner_prompts = [text for name, (_, text) in zip(runtime.nudged, runtime.nudge_calls) if name == owner.name]
    assert owner_prompts and "Service is back" in owner_prompts[0], owner_prompts


def test_a_successor_claim_during_restart_prevents_resuming_the_old_owner(store, tmp_path, monkeypatch):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    old = agents[2]
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.live.remove(old.name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)

    def takeover(repo, names):
        assert store.release("sw", old.task, old.name)
        assert store.claim("sw", old.task, "successor", 600_000)
        ledger.rows[old.task]["claimed_by"] = "successor"
        return {old.name: str(tmp_path)}

    monkeypatch.setattr(snapshot, "worktrees", takeover)
    launches = list(runtime.spawned)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert not runtime.resumed
    assert runtime.spawned == launches
    assert store.claimant("sw", old.task) == "successor"
    assert ledger.rows[old.task]["claimed_by"] == "successor"


@pytest.mark.parametrize("failure", ["unsupported", "ambiguous"])
def test_a_definite_resume_blocker_uses_the_handoff_but_an_ambiguous_launch_does_not_duplicate_it(
    store, tmp_path, monkeypatch, failure
):
    from scripts.swarm import snapshot

    ledger, runtime, agents = three_owners(store)
    old = agents[2]
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    handoff = "## Next\nContinue the current task from its recorded proof.\n"
    store.put_handoff("sw", old.task, handoff, seat=old.seat)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.live.remove(old.name)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)

    def blocked(config, agent, text):
        raise SpawnError("conversation resume blocked", failure)

    monkeypatch.setattr(runtime, "resume", blocked)
    launches = len(runtime.spawned)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    if failure == "unsupported":
        assert len(runtime.spawned) == launches + 1
        assert runtime.spawned[-1] == ("eng", old.name, old.task)
        assert runtime.tasks[-1]["handoff"] == handoff
        assert next(agent for agent in store.agents("sw") if agent.name == old.name).seat == old.seat
    else:
        assert len(runtime.spawned) == launches
    assert store.claimant("sw", old.task) == old.name
    assert ledger.rows[old.task]["claimed_by"] == old.name


def test_work_after_outage_closure_excludes_an_agent_that_only_becomes_idle_later(store):
    ledger, runtime, agents = three_owners(store)
    healthy = agents[0]
    runtime.statuses[healthy.name] = "working"
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    runtime.statuses[healthy.name] = "idle"
    idle.beat(store.redis, "sw", healthy.name, "idle", 10 * MINUTE)
    tick("sw", store, ledger, runtime, 10 * MINUTE)
    assert healthy.name not in runtime.nudged
    assert next(agent for agent in store.agents("sw") if agent.name == healthy.name).idle_ticks == 1


@pytest.mark.parametrize("error_state", ["idle", "waiting"])
def test_overlapping_outages_hold_recovery_and_persistent_errors_retry_three_times_then_free_the_seat(
    store, error_state
):
    from scripts.swarm.store import RedisStore

    ledger, runtime, agents = three_owners(store)
    stuck = agents[0]
    runtime.errors[stuck.name] = "provider unavailable"
    runtime.statuses[stuck.name] = error_state
    runtime.nudge_refusals[stuck.name] = "provider unavailable"
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    outage.observe("sw", "redis", ["Redis unavailable"], 8 * MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    tick("sw", store, ledger, runtime, 9 * MINUTE)
    assert not runtime.nudged
    outage.observe("sw", "redis", [], 10 * MINUTE)
    for at in (10, 11, 12):
        store = RedisStore(store.redis)
        tick("sw", store, ledger, runtime, at * MINUTE)
        idle.beat(store.redis, "sw", stuck.name, "idle", at * MINUTE + 1)
    assert runtime.nudged.count(stuck.name) == 3
    assert store.claimant("sw", stuck.task) == stuck.name
    tick("sw", store, ledger, runtime, 13 * MINUTE)
    assert runtime.nudged.count(stuck.name) == 3
    assert stuck.name in runtime.killed
    assert stuck.name not in {agent.name for agent in store.agents("sw")}
    assert store.claimant("sw", stuck.task) is None
    assert ledger.rows[stuck.task]["state"] == "open"


def test_a_new_task_generation_fences_the_recovery_pass(store):
    ledger, runtime, agents = three_owners(store)
    agent = next(agent for agent in store.agents("sw") if agent.name == agents[0].name)
    authority = {
        "state": "active",
        "holder": agent.name,
        "execution_id": agent.execution_id,
        "execution_generation": agent.generation,
        "generation": 1,
        "lease_deadline_ms": 20 * MINUTE,
    }
    key = store.key("sw", "task-authority", agent.task)
    store.redis.set(key, json.dumps(authority))
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    rows = {task["id"]: task for task in ledger.state("sw")["tasks"]}
    resume.track_outages("sw", store, runtime, rows, 8 * MINUTE)
    assert resume.recovery_pending(store, "sw", agent)
    authority["generation"] += 1
    store.redis.set(key, json.dumps(authority))
    outage.observe("sw", "provider", [], 9 * MINUTE)
    resume.recover_outages("sw", store, ledger, runtime, rows, 9 * MINUTE)
    assert not resume.recovery_pending(store, "sw", agent)
    assert agent.name not in runtime.nudged
    assert store.claimant("sw", agent.task) == agent.name


def rows_of(ledger):
    return {task["id"]: task for task in ledger.state("sw")["tasks"]}


def owners(store):
    return [agent for agent in store.agents("sw") if agent.lane == "eng"]


def recovery_state(store, name):
    raw = store.redis.hget(store.key("sw", resume.RECOVERY), name)
    return json.loads(raw)["state"] if raw else None


def pending_owners(store, tmp_path, monkeypatch, worktree=True):
    from scripts.swarm import snapshot

    ledger, runtime, _ = three_owners(store)

    def worktrees(repo, names):
        assert repo == store.config("sw").repo
        return {name: str(tmp_path) for name in names} if worktree else {}

    monkeypatch.setattr(snapshot, "worktrees", worktrees)
    state = ledger.state

    def own_state(slug):
        assert slug == "sw"
        return state(slug)

    monkeypatch.setattr(ledger, "state", own_state)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 8 * MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    return ledger, runtime, owners(store)


def take_over(store, ledger, agent):
    assert store.release("sw", agent.task, agent.name)
    assert store.claim("sw", agent.task, "successor", 600_000)
    ledger.rows[agent.task]["claimed_by"] = "successor"


@pytest.mark.parametrize(
    "change",
    [
        "missing row",
        "done",
        "not working",
        "ledger claimant",
        "store claimant",
        "seat",
        "released authority",
        "lapsed authority",
    ],
)
def test_an_owner_that_loses_its_task_drops_its_pending_recovery(store, tmp_path, monkeypatch, change):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    agent = agents[0]
    assert agent.seat and recovery_state(store, agent.name) == "pending"
    rows = rows_of(ledger)
    if change == "missing row":
        rows = {}
    elif change == "done":
        rows[agent.task]["done"] = True
    elif change == "not working":
        store.put_agent("sw", replace(agent, state="awaiting-decision"))
    elif change == "ledger claimant":
        rows[agent.task]["claimed_by"] = "successor"
    elif change == "store claimant":
        assert store.release("sw", agent.task, agent.name)
        assert store.claim("sw", agent.task, "successor", 600_000)
    elif change == "seat":
        store.seats.occupy(agent.seat, "successor", 9 * MINUTE)
    else:
        released = change == "released authority"
        authority = {
            "state": "released" if released else "active",
            "holder": agent.name,
            "execution_id": agent.execution_id,
            "execution_generation": agent.generation,
            "generation": 0,
            "lease_deadline_ms": 20 * MINUTE if released else 10 * MINUTE,
        }
        store.redis.set(store.key("sw", "task-authority", agent.task), json.dumps(authority))
    resume.track_outages("sw", store, runtime, rows, 10 * MINUTE)
    assert recovery_state(store, agent.name) is None
    assert not resume.recovery_pending(store, "sw", agent)


def test_a_working_heartbeat_after_closure_keeps_a_pending_recovery(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    idle.beat(store.redis, "sw", agents[0].name, idle.WORKING, 10 * MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 10 * MINUTE)
    assert resume.recovery_pending(store, "sw", agents[0])


def test_an_owner_restarted_after_the_outage_completes_its_recovery(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    store.put_agent("sw", replace(agents[0], started_at=10 * MINUTE))
    resume.track_outages("sw", store, runtime, rows_of(ledger), 10 * MINUTE)
    assert recovery_state(store, agents[0].name) == "done"
    assert recovery_state(store, agents[1].name) == "pending"


def test_an_owner_started_after_the_outage_does_not_stop_tracking_the_others(store):
    ledger, runtime, _ = three_owners(store)
    first, healthy, stuck = owners(store)
    store.put_agent("sw", replace(first, started_at=10 * MINUTE))
    runtime.statuses[healthy.name] = idle.WORKING
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 10 * MINUTE)
    assert [recovery_state(store, agent.name) for agent in (first, healthy, stuck)] == [None, "done", "pending"]


def test_an_unchanged_owner_does_not_stop_rerecording_a_later_owner_with_a_new_identity(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    last = agents[2]
    authority = {
        "state": "active",
        "holder": last.name,
        "execution_id": last.execution_id,
        "execution_generation": last.generation,
        "generation": 1,
        "lease_deadline_ms": 20 * MINUTE,
    }
    store.redis.set(store.key("sw", "task-authority", last.task), json.dumps(authority))
    assert not resume.recovery_pending(store, "sw", last)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 9 * MINUTE)
    assert resume.recovery_pending(store, "sw", agents[0])
    assert resume.recovery_pending(store, "sw", last)


def test_the_resume_prompt_names_the_task_workspace_and_keeps_the_claim(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    stuck = agents[1]
    ledger.rows[stuck.task]["workspace"] = "/work/folder"
    ledger.rows[agents[0].task]["workspace"] = ""
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    texts = dict(zip(runtime.nudged, (text for _, text in runtime.nudge_calls)))
    assert texts[stuck.name] == (
        f"Service is back after the outage. As {stuck.name}, re-read your task folder /work/folder "
        f"(steering.md, progress.md, proof.md), then run agentihooks msg inbox and continue your own task {stuck.task}. "
        "Keep your existing claim; do not claim new work."
    )
    assert f"re-read your task folder {ledger_workspace.folder('sw', agents[0].task)} (" in texts[agents[0].name]


def test_one_recovery_pass_completes_working_nudged_and_reopened_owners(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    working, nudged, dead = agents
    runtime.statuses[working.name] = idle.WORKING
    runtime.live.remove(dead.name)
    decision = {"profile": "engineer", "source": "test"}
    store.put_agent("sw", replace(dead, idle_ticks=2, profile_decision=decision))
    runtime.placement = {"model_source": "probe"}
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert sorted(actions) == sorted([f"outage resume for {nudged.name}", f"outage resume for {dead.name}"])
    assert [recovery_state(store, agent.name) for agent in agents] == ["done", "done", "done"]
    assert [config.slug for config in runtime.resume_configs] == ["sw"]
    reopened = next(agent for agent in owners(store) if agent.name == dead.name)
    assert (reopened.pane_id, reopened.started_at, reopened.idle_ticks) == ("recovered:pane", 9 * MINUTE, 0)
    assert (reopened.model, reopened.account, reopened.profile_decision) == ("sonnet", dead.account, decision)
    assert dead.effort and reopened.effort == dead.effort
    assert reopened.model_source == "probe"
    assert runtime.resumed[-1][2].startswith("Service is back after the outage.")


def test_a_refused_retry_waits_for_the_next_tick(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    stuck = agents[0]
    runtime.errors[stuck.name] = "provider unavailable"
    runtime.nudge_refusals[stuck.name] = "provider unavailable"
    for _ in range(2):
        resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert runtime.nudged.count(stuck.name) == 1


@pytest.mark.parametrize("conversation", ["fresh-conversation", ""])
def test_a_dead_owner_without_a_worktree_restarts_from_its_last_transfer_handoff(
    store, tmp_path, monkeypatch, conversation
):
    from scripts.handoff import transfers

    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch, worktree=False)
    dead = agents[2]
    runtime.live.remove(dead.name)
    handoff = "## Next\nContinue from the saved proof.\n"
    transfers.record(store, "sw", dead, "quota", handoff, 2 * MINUTE)
    store.put_agent("sw", replace(dead, idle_ticks=2))
    native_spawn = runtime.spawn

    def spawn(config, lane, name, task):
        return replace(native_spawn(config, lane, name, task), conversation_id=conversation)

    monkeypatch.setattr(runtime, "spawn", spawn)
    launches = len(runtime.spawned)
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert f"outage resume for {dead.name}" in actions
    assert not runtime.resumed
    assert runtime.spawned[launches:] == [("eng", dead.name, dead.task)]
    assert runtime.tasks[-1]["handoff"] == handoff
    transfer = runtime.tasks[-1]["transfer"]
    assert transfer == transfers.get(store, "sw", transfer["id"])
    assert (transfer["reason"], transfer["successor"], transfer["handoff"]) == ("outage", dead.name, handoff)
    assert handoff in "\n".join(prompt.priming_lines(runtime.tasks[-1]))
    assert runtime.spawn_configs[-1] == "sw"
    assert runtime.tasks[-1]["seat"] == dead.seat
    restarted = next(agent for agent in owners(store) if agent.name == dead.name)
    assert (restarted.pane_id, restarted.conversation_id, restarted.started_at, restarted.idle_ticks) == (
        f"w1:p{launches + 1}",
        conversation,
        9 * MINUTE,
        0,
    )


def test_a_dead_owner_whose_handoff_lost_its_run_options_restarts_fresh_on_its_seat(store, tmp_path, monkeypatch):
    from scripts.handoff import transfers

    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch, worktree=False)
    dead = agents[2]
    runtime.live.remove(dead.name)
    handoff = "## Next\nContinue from the saved proof.\n"
    transfers.record(store, "sw", dead, "quota", handoff, 2 * MINUTE)
    native_spawn, launched = runtime.spawn, []

    def spawn(config, lane, name, task):
        launched.append(profile_choice.transfer(task))
        return native_spawn(config, lane, name, task)

    monkeypatch.setattr(runtime, "spawn", spawn)
    launches = len(runtime.spawned)
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert f"outage resume for {dead.name}" in actions
    assert launched == [{}]
    assert runtime.spawned[launches:] == [("eng", dead.name, dead.task)]
    fresh = runtime.tasks[-1]
    assert (fresh["seat"], fresh["handoff"], fresh["transfer"]["handoff"]) == (dead.seat, handoff, handoff)
    assert handoff in "\n".join(prompt.priming_lines(fresh))
    assert recovery_state(store, dead.name) == "done"


def test_a_restart_refused_for_another_reason_is_reported(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch, worktree=False)
    dead = agents[2]
    runtime.live.remove(dead.name)
    refused = []

    def spawn(config, lane, name, task):
        refused.append(task)
        raise SpawnError("no seat", "unavailable")

    monkeypatch.setattr(runtime, "spawn", spawn)
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert f"outage resume for {dead.name} refused: no seat" in actions
    assert len(refused) == 1


@pytest.mark.parametrize("moment", ["before resume", "during resume", "before respawn"])
def test_ownership_lost_during_recovery_is_reported(store, tmp_path, monkeypatch, moment):
    from scripts.handoff import transfers
    from scripts.swarm import snapshot

    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch, worktree=False)
    dead = agents[2]
    runtime.live.remove(dead.name)
    if moment == "before resume":

        def worktrees(repo, names):
            take_over(store, ledger, dead)
            return {dead.name: str(tmp_path)}

        monkeypatch.setattr(snapshot, "worktrees", worktrees)
    elif moment == "during resume":
        monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {dead.name: str(tmp_path)})

        def unsupported(config, agent, text):
            take_over(store, ledger, dead)
            raise SpawnError("conversation resume blocked", "unsupported")

        monkeypatch.setattr(runtime, "resume", unsupported)
    else:
        attach = transfers.attach

        def taken(store_, slug, agent):
            take_over(store, ledger, dead)
            return attach(store_, slug, agent)

        monkeypatch.setattr(transfers, "attach", taken)
    launches = list(runtime.spawned)
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert f"outage resume for {dead.name} refused: task ownership changed" in actions
    assert not runtime.resumed
    assert runtime.spawned == launches


@pytest.mark.parametrize(
    "message, reported", [("conversation resume blocked", "conversation resume blocked"), ("", "SpawnError")]
)
def test_an_ambiguous_resume_failure_reports_its_error(store, tmp_path, monkeypatch, message, reported):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    dead = agents[2]
    runtime.live.remove(dead.name)

    def ambiguous(config, agent, text):
        raise SpawnError(message, "ambiguous")

    monkeypatch.setattr(runtime, "resume", ambiguous)
    actions = resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert f"outage resume for {dead.name} refused: {reported}" in actions


def test_an_owner_whose_task_is_in_review_is_recovered(store):
    ledger, runtime, agents = three_owners(store)
    ledger.rows[agents[0].task]["state"] = "pr"
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 8 * MINUTE)
    assert recovery_state(store, agents[0].name) == "pending"


def test_an_outage_ending_at_the_owner_start_counts_toward_recovery(store):
    ledger, runtime, _ = three_owners(store)
    first = owners(store)[0]
    store.put_agent("sw", replace(first, started_at=0))
    outage.observe("sw", "provider", ["provider unavailable"], 0)
    outage.observe("sw", "provider", [], 0)
    resume.track_outages("sw", store, runtime, rows_of(ledger), MINUTE)
    assert recovery_state(store, first.name) == "pending"


@pytest.mark.parametrize("now_ms", [1, 9 * MINUTE])
def test_a_new_recovery_is_due_on_its_first_pass_at_any_clock(store, tmp_path, monkeypatch, now_ms):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), now_ms)
    assert agents[0].name in runtime.nudged


@pytest.mark.parametrize("pending_input", ["typed", "title"])
def test_an_owner_with_pending_input_is_not_prompted(store, tmp_path, monkeypatch, pending_input):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    if pending_input == "typed":
        runtime.inputs[agents[0].name] = "half typed reply"
    else:
        runtime.titles[agents[0].name] = "Approve this edit?"
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert agents[0].name not in runtime.nudged
    assert agents[1].name in runtime.nudged
    assert resume.recovery_pending(store, "sw", agents[0])


def test_a_reopened_conversation_records_the_placement_it_reports(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    dead = agents[2]
    runtime.live.remove(dead.name)
    store.put_agent("sw", replace(dead, model_source="ledger", profile_decision={"profile": "engineer"}))
    runtime.placement = {"account": "acct-2", "effort": "max", "profile_decision": {"profile": "reviewer"}}
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    reopened = next(agent for agent in owners(store) if agent.name == dead.name)
    assert (reopened.account, reopened.effort, reopened.model_source, reopened.profile_decision) == (
        "acct-2",
        "max",
        "ledger",
        {"profile": "reviewer"},
    )


def test_a_reopened_conversation_keeps_the_saved_placement_fallback(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    dead = agents[2]
    runtime.live.remove(dead.name)
    saved = {"profile": "engineer", "placement_fallback": "no hive places the saved codex harness"}
    store.put_agent("sw", replace(dead, profile_decision=saved))
    runtime.placement = {"profile_decision": {"profile": "reviewer"}}
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    reopened = next(agent for agent in owners(store) if agent.name == dead.name)
    assert reopened.profile_decision == {
        "profile": "reviewer",
        "placement_fallback": "no hive places the saved codex harness",
    }


def test_a_pending_owner_missing_its_pane_does_not_stop_other_conversation_updates(store):
    ledger, runtime, _ = three_owners(store)
    first, second, third = owners(store)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    runtime.conversation_ids = {second.pane_id: "conversation-new"}
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    found = {agent.name: agent.conversation_id for agent in owners(store)}
    assert found == {first.name: "conversation-0", second.name: "conversation-new", third.name: "conversation-2"}


def test_a_non_owner_does_not_stop_tracking_later_owners(store):
    ledger, runtime, _ = three_owners(store)
    first, second, _ = owners(store)
    ledger.rows[first.task]["done"] = True
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 8 * MINUTE)
    assert [recovery_state(store, agent.name) for agent in (first, second)] == [None, "pending"]


def test_an_owner_that_worked_after_the_outage_is_not_recovered(store):
    ledger, runtime, _ = three_owners(store)
    first, second, _ = owners(store)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    idle.beat(store.redis, "sw", first.name, idle.IDLE, 10 * MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 10 * MINUTE)
    assert [recovery_state(store, agent.name) for agent in (first, second)] == [None, "pending"]


def test_an_owner_holding_a_declared_wait_is_not_stalled(store):
    ledger, runtime, _ = three_owners(store)
    first, second, _ = owners(store)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    outage.observe("sw", "provider", [], 9 * MINUTE)
    idle.declare_wait(store.redis, "sw", first.name, 60 * MINUTE, "checks", 8 * MINUTE)
    resume.track_outages("sw", store, runtime, rows_of(ledger), 10 * MINUTE)
    assert [recovery_state(store, agent.name) for agent in (first, second)] == ["done", "pending"]


def test_the_tick_tracks_recovery_on_its_own_clock(store):
    ledger, runtime, _ = three_owners(store)
    first, second, _ = owners(store)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    idle.beat(store.redis, "sw", first.name, idle.WORKING, 7 * MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    assert [recovery_state(store, agent.name) for agent in (first, second)] == [None, "pending"]


def test_a_pending_recovery_stops_once_its_attempts_are_spent(store, tmp_path, monkeypatch):
    _, _, agents = pending_owners(store, tmp_path, monkeypatch)
    key = store.key("sw", resume.RECOVERY)
    record = json.loads(store.redis.hget(key, agents[0].name))
    record["attempts"] = resume.RECOVERY_ATTEMPTS
    store.redis.hset(key, agents[0].name, json.dumps(record))
    assert not resume.recovery_pending(store, "sw", agents[0])


@pytest.mark.parametrize("outage_state", ["closed", "open"])
def test_a_pending_recovery_after_its_outage_closes_retires_an_idle_holder_while_paused(
    store, tmp_path, monkeypatch, outage_state
):
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    monkeypatch.setattr(resume, "recover_outages", lambda *args: [])
    holder = agents[0]
    store.put_agent("sw", replace(holder, idle_ticks=IDLE_KILL_TICKS - 1))
    for agent in agents[1:]:
        runtime.statuses[agent.name] = "working"
    if outage_state == "open":
        outage.observe("sw", "provider", ["provider unavailable"], 10 * MINUTE)
    assert store.config("sw").state == "paused"
    assert resume.recovery_pending(store, "sw", holder)
    actions = tick("sw", store, ledger, runtime, 40 * MINUTE)
    if outage_state == "open":
        assert holder.name not in runtime.killed
        assert store.claimant("sw", holder.task) == holder.name
        return
    assert runtime.killed == [holder.name]
    assert (ledger.rows[holder.task]["state"], ledger.rows[holder.task]["claimed_by"]) == ("open", "")
    stalled = [action for action in actions if action.startswith(f"stalled {holder.name}")]
    assert stalled and "outage recovery could not proceed" in stalled[0], actions


def test_a_pending_recovery_still_holds_an_idle_holder_just_after_its_outage_closes(store, tmp_path, monkeypatch):
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    monkeypatch.setattr(resume, "recover_outages", lambda *args: [])
    holder = agents[0]
    store.put_agent("sw", replace(holder, idle_ticks=IDLE_KILL_TICKS - 1))
    tick("sw", store, ledger, runtime, 10 * MINUTE)
    assert holder.name not in runtime.killed
    assert store.claimant("sw", holder.task) == holder.name


def test_an_owner_holding_its_task_authority_is_recovered(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    agent = agents[0]
    authority = {
        "state": "active",
        "holder": agent.name,
        "execution_id": agent.execution_id,
        "execution_generation": agent.generation,
        "generation": 0,
        "lease_deadline_ms": 20 * MINUTE,
    }
    store.redis.set(store.key("sw", "task-authority", agent.task), json.dumps(authority))
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert agent.name in runtime.nudged
    assert recovery_state(store, agent.name) == "done"


def test_a_refused_prompt_keeps_the_recovery_pending(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    runtime.nudge_refusals[agents[0].name] = "pane busy"
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert agents[0].name in runtime.nudged
    assert resume.recovery_pending(store, "sw", agents[0])


def test_a_non_owner_does_not_stop_later_recoveries(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    ledger.rows[agents[0].task]["done"] = True
    resume.recover_outages("sw", store, ledger, runtime, rows_of(ledger), 9 * MINUTE)
    assert agents[0].name not in runtime.nudged
    assert agents[1].name in runtime.nudged


def test_an_owner_already_prompted_this_pass_does_not_stop_later_recoveries(store, tmp_path, monkeypatch):
    ledger, runtime, agents = pending_owners(store, tmp_path, monkeypatch)
    runtime.nudge_refusals[agents[0].name] = "pane busy"
    rows = rows_of(ledger)
    first_only = {task: {**row, "done": task != agents[0].task} for task, row in rows.items()}
    resume.recover_outages("sw", store, ledger, runtime, first_only, 9 * MINUTE)
    resume.recover_outages("sw", store, ledger, runtime, rows, 9 * MINUTE)
    assert runtime.nudged.count(agents[0].name) == 1
    assert agents[1].name in runtime.nudged


def test_a_dead_owner_pending_recovery_keeps_its_claim_lease(store):
    from scripts.swarm.tick import LEASE_MS

    ledger, runtime, _ = three_owners(store)
    dead = owners(store)[2]
    runtime.live.remove(dead.name)
    claim = store.key("sw", "claim", dead.task)
    store.redis.pexpire(claim, 60_000)
    outage.observe("sw", "provider", ["provider unavailable"], MINUTE)
    tick("sw", store, ledger, runtime, 8 * MINUTE)
    assert store.claimant("sw", dead.task) == dead.name
    assert store.redis.pttl(claim) > LEASE_MS // 2
