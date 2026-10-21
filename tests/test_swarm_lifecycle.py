import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import command_runner, commands, lifecycle
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server


def queue_class():
    from scripts.swarm_v2.api.commands import CommandQueue

    return CommandQueue


SLUG = "life"
CHECKPOINT = {"status": "checkpointed", "checkpoint": "ckpt-eng-1"}
STAMPS = ("_meta.stamps",)
STALE = "the page is stale: this agent was replaced, finished or moved task; wait for the next swarm view"


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 2, 0, state="running"))
    return store


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(command_runner, "run", lambda argv, env: calls.append(argv) or "")
    return calls


def kube(store, seat="eng-1@life", task="t1"):
    target = {"pod_namespace": "workers", "pod_name": seat.split("@")[0], "pod_uid": f"uid-{seat}"}
    agent = AgentRecord(store.next_name(SLUG, "eng"), "eng", task, seat=seat, runtime_backend="kubernetes")
    return store.start_execution(SLUG, replace(agent, runtime_target=target))


def local(store, seat="eng-2@life", task="t2"):
    target = {"server_id": "hive", "pid": 4321, "pid_start": 99}
    agent = AgentRecord(store.next_name(SLUG, "eng"), "eng", task, seat=seat, runtime_target=target)
    return store.start_execution(SLUG, agent)


def successor(store, agent):
    target = {**agent.runtime_target, "pod_uid": "uid-next"} if "pod_uid" in agent.runtime_target else {}
    target = target or {**agent.runtime_target, "pid": 4322}
    fresh = replace(agent, execution_id="", generation=0, runtime_target=target)
    return store.start_execution(SLUG, fresh, agent.execution_id)


def body(agent, control, op="op-first-1", revisions=None):
    return {
        "action": "lifecycle",
        "control": control,
        "agent": agent.name,
        "op": op,
        **lifecycle.seen(agent, revisions or {}),
    }


def publish(store, revisions):
    commands.publish(store, SLUG, {"task_revisions": revisions}, {})


def ask(store, agent, control, op="op-first-1", revisions=None, operator=True, environ=None):
    return lifecycle.request(store, SLUG, body(agent, control, op, revisions), operator=operator, environ=environ or {})


def refused(store, agent, control, **kwargs):
    with pytest.raises(lifecycle.Refused) as caught:
        ask(store, agent, control, **kwargs)
    return caught.value.status, str(caught.value)


def queued(store):
    return [row for row in commands.rows(store, SLUG) if row["command"] == lifecycle.COMMAND]


def test_the_control_table_names_scope_and_consequence_for_all_five():
    table = {c.action: (c.label, c.scope, c.destructive) for c in lifecycle.CONTROLS}
    assert table == {
        "attach": ("attach", "terminal view", False),
        "pause": ("pause", "one swarm", False),
        "drain": ("drain", "one agent", False),
        "cancel": ("cancel", "one agent", True),
        "force_stop": ("force stop", "one agent", True),
    }
    assert len({c.consequence for c in lifecycle.CONTROLS}) == 5
    assert lifecycle.AGENT_ACTIONS == ("attach", "drain", "cancel", "force_stop")


def test_task_revisions_read_only_each_task_state_stamp():
    stamps = {
        "tasks/t1/state": {"rev": 7},
        "tasks/t1/text": {"rev": 9},
        "tasks/t2/state": {},
        "chat": {"rev": 3},
        "tasks/t3/state/x": {"rev": 4},
    }
    assert lifecycle.revisions(stamps) == {"t1": 7, "t2": 0}


def test_offers_disable_unsupported_controls_per_backend(store):
    remote, near = kube(store), local(store)
    publish(store, {"t1": 4})
    view = lifecycle.view(store, SLUG, {})
    assert view["agents"][remote.name] == {
        "incarnation": lifecycle.incarnation(remote),
        "generation": 1,
        "task_revision": 4,
        "pending": "",
        "disabled": {"attach": "", "drain": "", "cancel": "", "force_stop": "a kubernetes agent has no force stop"},
        "terminate": False,
    }
    assert view["agents"][near.name]["disabled"] == {
        "attach": "a local agent has no attach",
        "drain": "a local agent has no drain",
        "cancel": "a local agent has no cancel",
        "force_stop": "",
    }
    assert view["agents"][near.name]["task_revision"] == 0
    assert [c["action"] for c in view["controls"]] == ["attach", "pause", "drain", "cancel", "force_stop"]
    assert view["destructive"] is True
    assert view["ledger_control_conflicts_total"] == 0


def test_a_finished_agent_or_one_without_execution_identity_offers_nothing(store):
    agent = kube(store)
    assert set(lifecycle.offer(replace(agent, state="finished"), {}, "", True)["disabled"].values()) == {
        "the agent has finished"
    }
    bare = replace(agent, execution_id="")
    assert lifecycle.disabled(bare, "drain", "", True) == "the agent has no execution identity"
    assert lifecycle.disabled(agent, "drain", "op-x", True) == "an earlier control on this agent is still unresolved"


def test_the_rollback_switch_turns_off_cancel_and_force_stop_only(store):
    remote, near = kube(store), local(store)
    off = {lifecycle.SWITCH: " OFF "}
    view = lifecycle.view(store, SLUG, off)
    assert view["destructive"] is False
    assert view["agents"][remote.name]["disabled"]["cancel"] == "cancel is turned off; terminate still works"
    assert view["agents"][remote.name]["disabled"]["drain"] == ""
    assert view["agents"][near.name]["disabled"]["force_stop"] == "force stop is turned off; terminate still works"
    assert refused(store, near, "force_stop", environ=off) == (422, "force stop is turned off; terminate still works")
    assert lifecycle.destructive_on({lifecycle.SWITCH: "on"}) is True
    assert lifecycle.destructive_on({}) is True


def test_a_drain_is_recorded_and_queued_once_and_a_repeat_replays_it(store):
    agent = kube(store)
    first = ask(store, agent, "drain")
    assert {k: first[k] for k in ("op", "control", "agent", "state", "generation", "execution_id")} == {
        "op": "op-first-1",
        "control": "drain",
        "agent": agent.name,
        "state": "queued",
        "generation": 1,
        "execution_id": agent.execution_id,
    }
    [row] = queued(store)
    assert row["argv"] == ["drain", agent.name, "op-first-1"]
    assert first["command"] == row["id"]
    assert ask(store, agent, "drain") == first
    assert len(queued(store)) == 1
    view = lifecycle.view(store, SLUG, {})
    assert view["agents"][agent.name]["pending"] == "op-first-1"
    assert [op["op"] for op in view["operations"]] == ["op-first-1"]
    assert "fingerprint" not in view["operations"][0]
    assert view["ledger_control_conflicts_total"] == 0


def test_the_same_operation_id_with_another_control_is_a_counted_conflict(store):
    agent = kube(store)
    ask(store, agent, "drain")
    assert refused(store, agent, "cancel") == (409, "the operation id was already sent with another control")
    assert lifecycle.conflicts(store, SLUG) == {"reused": 1}
    assert lifecycle.view(store, SLUG, {})["conflicts"] == {"reused": 1}
    assert len(queued(store)) == 1


def test_a_new_click_waits_until_the_earlier_operation_settles(store, ran):
    agent = kube(store)
    ask(store, agent, "drain")
    assert refused(store, agent, "cancel", op="op-second") == (
        409,
        "an earlier control on this agent is still unresolved",
    )
    assert lifecycle.conflicts(store, SLUG) == {"pending": 1}
    command_runner.consume(store, SLUG)
    assert refused(store, agent, "cancel", op="op-second")[0] == 409
    finish(store, agent, CHECKPOINT)
    assert ask(store, agent, "cancel", op="op-second")["state"] == "queued"


def test_an_old_force_stop_after_replacement_is_stale_and_leaves_the_successor(store, ran):
    agent = local(store)
    stale = body(agent, "force_stop")
    next_one = successor(store, agent)
    before = {key: store.redis.dump(key) for key in store.redis.keys("*")}
    with pytest.raises(lifecycle.Refused) as caught:
        lifecycle.request(store, SLUG, stale, operator=True, environ={})
    assert caught.value.status == 409
    assert str(caught.value) == (
        "the page is stale: this agent was replaced, finished or moved task; wait for the next swarm view"
    )
    after = {key: store.redis.dump(key) for key in store.redis.keys("*") if not key.endswith("lifecycle-conflicts")}
    assert after == before
    assert lifecycle.conflicts(store, SLUG) == {"stale": 1}
    assert queued(store) == []
    assert ran == []
    assert store.execution(SLUG, next_one.execution_id).generation == 2


def test_a_moved_task_revision_or_a_gone_agent_is_stale(store):
    agent = kube(store)
    publish(store, {"t1": 4})
    with pytest.raises(lifecycle.Refused) as caught:
        lifecycle.request(store, SLUG, body(agent, "drain", revisions={"t1": 3}), operator=True, environ={})
    assert caught.value.status == 409
    gone = {**body(agent, "drain"), "agent": "engineer@nobody-0001"}
    with pytest.raises(lifecycle.Refused):
        lifecycle.request(store, SLUG, gone, operator=True, environ={})
    assert lifecycle.conflicts(store, SLUG) == {"stale": 2}


def test_an_unsupported_attach_target_is_refused_without_a_conflict(store):
    agent = local(store)
    assert refused(store, agent, "attach") == (422, "a local agent has no attach")
    assert lifecycle.conflicts(store, SLUG) == {}
    assert queued(store) == []


def test_destructive_controls_need_the_operator(store):
    agent = kube(store)
    assert refused(store, agent, "cancel", operator=False) == (403, "cancel needs the operator")
    assert ask(store, agent, "drain", operator=False)["state"] == "queued"


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"control": "pause"}, "control must be one of attach, drain, cancel, force_stop"),
        ({"agent": "-x y"}, "a lifecycle control needs an exact agent name"),
        ({"op": "short"}, "a lifecycle control needs an operation id"),
        ({"generation": -1}, "a lifecycle control needs the incarnation, generation and task revision the page showed"),
        (
            {"task_revision": "1"},
            "a lifecycle control needs the incarnation, generation and task revision the page showed",
        ),
        ({"incarnation": 5}, "a lifecycle control needs the incarnation, generation and task revision the page showed"),
    ],
)
def test_malformed_requests_are_refused_before_any_lookup(store, change, text):
    agent = kube(store)
    with pytest.raises(lifecycle.Refused) as caught:
        lifecycle.request(store, SLUG, {**body(agent, "drain"), **change}, operator=True, environ={})
    assert (caught.value.status, str(caught.value)) == (400, text)
    with pytest.raises(lifecycle.Refused) as caught:
        lifecycle.request(store, SLUG, ["drain"], operator=True, environ={})
    assert (caught.value.status, str(caught.value)) == (400, "a lifecycle control is a JSON object")


def test_the_runner_drains_one_worker_and_reports_its_checkpoint(store, ran):
    agent, other = kube(store), kube(store, "eng-3@life", "t3")
    ask(store, agent, "drain")
    assert command_runner.consume(store, SLUG) == ["control lifecycle acknowledged"]
    queue = queue_class()(store, SLUG)
    [issued] = queue.pending(agent.execution_id, 5)
    assert (issued["kind"], issued["generation"]) == ("drain", 1)
    assert queue.pending(other.execution_id, 5) == []
    queue.accept(agent.execution_id, issued["command_id"], issued["payload_digest"])
    queue.complete(agent.execution_id, issued["command_id"], CHECKPOINT)
    view = lifecycle.view(store, SLUG, {})
    [op] = view["operations"]
    assert (op["state"], op["error"]) == ("issued", "")
    assert op["result"] == {"worker_command": issued["command_id"]}
    assert op["worker"] == {"state": "completed", "outcome": CHECKPOINT}
    assert view["agents"][agent.name]["pending"] == ""
    assert {a.name: a.state for a in store.agents(SLUG)} == {agent.name: "working", other.name: "working"}
    assert ran == []


def test_the_runner_fences_a_control_whose_agent_was_replaced_after_queueing(store, ran):
    agent = local(store)
    ask(store, agent, "force_stop")
    successor(store, agent)
    assert command_runner.consume(store, SLUG) == ["control lifecycle failed"]
    [op] = lifecycle.operations(store, SLUG)
    assert (op["state"], op["error"]) == ("failed", "the agent was replaced before the control ran")
    assert ran == []
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def test_the_runner_force_stops_a_local_agent_after_a_dry_run(store, ran):
    agent = local(store)
    ask(store, agent, "force_stop")
    command_runner.consume(store, SLUG)
    assert ran == [
        ["terminate-agent", agent.name, "--type", "any", "--dry-run"],
        ["terminate-agent", agent.name, "--type", "any"],
    ]
    assert lifecycle.operations(store, SLUG)[0]["state"] == "applied"


def test_the_runner_attaches_through_the_worker_terminal_command(store, ran):
    agent = kube(store)
    ask(store, agent, "attach")
    command_runner.consume(store, SLUG)
    assert ran == [["swarm", SLUG, "terminal", agent.execution_id]]


def test_a_refused_worker_command_fails_the_operation(store, ran, monkeypatch):
    from scripts.swarm_v2.auth_context import GrantRefused

    def refuse(*args):
        raise GrantRefused("stale_generation", "the execution is not the current occupant of its seat")

    agent = kube(store)
    ask(store, agent, "cancel")
    monkeypatch.setattr(queue_class(), "issue", refuse)
    command_runner.consume(store, SLUG)
    op = lifecycle.operations(store, SLUG)[0]
    assert (op["state"], op["error"]) == ("failed", "the execution is not the current occupant of its seat")


def test_execute_refuses_rows_that_do_not_match_their_operation(store, ran):
    agent = kube(store)
    record = ask(store, agent, "drain")
    assert lifecycle.execute(store, SLUG, {"argv": ["drain", agent.name, "op-missing"]}, ran.append) == (
        "no such lifecycle operation"
    )
    assert lifecycle.execute(store, SLUG, {"argv": ["cancel", agent.name, record["op"]]}, ran.append) == (
        "the lifecycle operation does not match its command"
    )
    assert lifecycle.operations(store, SLUG)[0]["state"] == "queued"


def test_a_settled_operation_keeps_a_newer_pending_one(store):
    agent = kube(store)
    record = ask(store, agent, "drain")
    store.redis.hset(store.key(SLUG, "lifecycle-pending"), agent.name, "op-newer-1")
    lifecycle.execute(store, SLUG, {"argv": ["drain", agent.name, record["op"]]}, lambda argv: "")
    assert store.redis.hget(store.key(SLUG, "lifecycle-pending"), agent.name) == "op-newer-1"


def test_the_swarm_view_carries_the_published_task_revision(store):
    agent = kube(store)
    assert commands.view(store, SLUG)["lifecycle"]["agents"][agent.name]["task_revision"] == 0
    publish(store, {"t1": 2})
    assert commands.view(store, SLUG)["lifecycle"]["agents"][agent.name]["task_revision"] == 2


def test_the_status_report_publishes_each_task_state_revision(store, monkeypatch):
    from scripts.swarm import status

    monkeypatch.setattr(status, "page_quota", lambda: {})
    stamps = {"tasks/t1/state": {"rev": 5}, "tasks/t1/text": {"rev": 8}}
    report = status.status_report(store, SLUG, {"tasks": [], "_meta": {"stamps": stamps}})
    assert report["task_revisions"] == {"t1": 5}
    assert status.status_report(store, SLUG, {"tasks": []})["task_revisions"] == {}


def test_the_ledger_server_queues_through_lifecycle_and_returns_fresh_status(store, monkeypatch):
    agent = kube(store)
    publish(store, {"t1": 3})
    stamps = {"_meta": {"stamps": {"tasks/t1/state": {"rev": 5}}}}
    monkeypatch.setattr(ledger_server.repository, "read", lambda slug, *keys: {(SLUG, STAMPS): stamps}[(slug, keys)])
    monkeypatch.setattr(ledger_server, "swarm_store", lambda: store)
    monkeypatch.setattr(ledger_server, "swarm_status", lambda slug: {"swarm": slug})
    assert ledger_server.lifecycle_control(SLUG, body(agent, "drain", revisions={"t1": 5}), True) == (
        {"swarm": SLUG},
        "",
    )
    assert len(queued(store)) == 1
    with pytest.raises(lifecycle.Refused):
        ledger_server.lifecycle_control(SLUG, body(agent, "drain", "op-other-1", revisions={"t1": 4}), True)
    with pytest.raises(lifecycle.Refused) as caught:
        ledger_server.lifecycle_control(SLUG, body(agent, "cancel", "op-other-2", revisions={"t1": 5}), False)
    assert caught.value.status == 403
    assert ledger_server.lifecycle_control("nowhere", body(agent, "drain"), True)[0] is None

    def missing(slug, *keys):
        raise KeyError(slug)

    monkeypatch.setattr(ledger_server.repository, "read", missing)
    assert ledger_server.lifecycle_control(SLUG, body(agent, "drain"), True) == (None, "no such ledger")


def test_submit_still_queues_one_pending_row(store):
    row = commands.submit(store, SLUG, "swarm", ["pause"])
    assert json.loads(store.redis.hget(store.key(SLUG, "commands"), row["id"]))["argv"] == ["pause"]
    assert store.redis.lrange(store.key(SLUG, "command-pending"), 0, -1) == [row["id"]]
    assert store.redis.lrange(store.key(SLUG, "command-order"), 0, -1) == [row["id"]]


def test_terminate_is_offered_only_under_the_rollback_switch(store):
    remote, near = kube(store), local(store)
    on, off = (
        lifecycle.view(store, SLUG, {})["agents"],
        lifecycle.view(store, SLUG, {lifecycle.SWITCH: "off"})["agents"],
    )
    assert (on[remote.name]["terminate"], on[near.name]["terminate"]) == (False, False)
    assert (off[remote.name]["terminate"], off[near.name]["terminate"]) == (True, True)


def age(store, op, by_ms):
    key = store.key(SLUG, "lifecycle-ops")
    record = json.loads(store.redis.hget(key, op))
    store.redis.hset(key, op, json.dumps({**record, "at": record["at"] - by_ms}))


def test_an_expired_control_stops_blocking_and_never_runs(store, ran):
    agent = local(store)
    ask(store, agent, "force_stop")
    age(store, "op-first-1", lifecycle.PENDING_MS)
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""
    assert ask(store, agent, "force_stop", op="op-second")["state"] == "queued"
    command_runner.consume(store, SLUG)
    states = {op["op"]: (op["state"], op["error"]) for op in lifecycle.operations(store, SLUG)}
    assert states == {
        "op-first-1": ("failed", "the control expired before the controller ran it"),
        "op-second": ("applied", ""),
    }
    assert len(ran) == 2


def test_a_control_still_fresh_just_before_expiry_keeps_blocking(store):
    agent = kube(store)
    ask(store, agent, "drain")
    age(store, "op-first-1", lifecycle.PENDING_MS - 60_000)
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == "op-first-1"


def test_a_destructive_control_turned_off_after_queueing_does_not_run(store, ran):
    agent = local(store)
    record = ask(store, agent, "force_stop")
    row = {"argv": ["force_stop", agent.name, record["op"]]}
    assert lifecycle.execute(store, SLUG, row, ran.append, {lifecycle.SWITCH: "off"}) == (
        "force stop was turned off before the control ran"
    )
    assert ran == []
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def test_an_unexpected_error_settles_the_operation_and_releases_the_agent(store, monkeypatch):
    def broken(*args):
        raise RuntimeError("queue offline")

    agent = kube(store)
    record = ask(store, agent, "drain")
    monkeypatch.setattr(queue_class(), "issue", broken)
    row = {"argv": ["drain", agent.name, record["op"]]}
    assert lifecycle.execute(store, SLUG, row, lambda argv: "") == "RuntimeError: queue offline"
    op = lifecycle.operations(store, SLUG)[0]
    assert (op["state"], op["error"]) == ("failed", "RuntimeError: queue offline")
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def test_settling_reports_when_the_records_keep_changing(store, monkeypatch):
    from redis.exceptions import WatchError

    from scripts.swarm.store import SwarmError

    real = store.redis.pipeline

    class Busy:
        def __init__(self, *args, **kwargs):
            self.pipe = real(*args, **kwargs)

        def __enter__(self):
            self.pipe.__enter__()
            return self

        def __exit__(self, *exc):
            return self.pipe.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def watch(self, *keys):
            raise WatchError("busy")

    agent = kube(store)
    record = ask(store, agent, "drain")
    monkeypatch.setattr(store.redis, "pipeline", Busy)
    with pytest.raises(SwarmError) as caught:
        lifecycle.execute(store, SLUG, {"argv": ["drain", agent.name, record["op"]]}, lambda argv: "")
    assert str(caught.value) == "lifecycle controls kept changing; the outcome was recorded without its watch"
    saved = json.loads(store.redis.hget(store.key(SLUG, "lifecycle-ops"), record["op"]))
    assert saved["state"] == "failed"
    assert saved["error"] == "commands kept changing; nothing was recorded"
    monkeypatch.undo()
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def finish(store, agent, outcome):
    queue = queue_class()(store, SLUG)
    [issued] = queue.pending(agent.execution_id, 5)
    queue.accept(agent.execution_id, issued["command_id"], issued["payload_digest"])
    queue.complete(agent.execution_id, issued["command_id"], outcome)


def test_a_worker_command_holds_the_agent_until_its_outcome(store, ran):
    agent = kube(store)
    ask(store, agent, "drain")
    command_runner.consume(store, SLUG)
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == "op-first-1"
    finish(store, agent, {"status": "failed"})
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def test_an_expired_or_overlong_worker_command_releases_the_agent(store, ran):
    agent = kube(store)
    ask(store, agent, "drain")
    command_runner.consume(store, SLUG)
    key = queue_class()(store, SLUG).key(agent.execution_id)
    [(command, raw)] = store.redis.hgetall(key).items()
    store.redis.hset(key, command, json.dumps({**json.loads(raw), "expires_at_ms": 0}))
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""
    store.redis.hset(key, command, raw)
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == "op-first-1"
    age(store, "op-first-1", lifecycle.WORKER_HOLD_MS)
    assert lifecycle.view(store, SLUG, {})["agents"][agent.name]["pending"] == ""


def test_admission_fences_on_the_revisions_it_is_given_over_the_published_ones(store):
    agent = kube(store)
    publish(store, {"t1": 3})
    with pytest.raises(lifecycle.Refused):
        lifecycle.request(
            store, SLUG, body(agent, "drain", revisions={"t1": 3}), operator=True, environ={}, task_revisions={"t1": 4}
        )
    assert ask(store, agent, "drain", revisions={"t1": 3})["state"] == "queued"


def racing(monkeypatch, store, part, times):
    real, left, key = store.redis.pipeline, [times], store.key(SLUG, f"lifecycle-{part}")

    class Racing:
        def __init__(self, *args, **kwargs):
            self.pipe = real(*args, **kwargs)

        def __enter__(self):
            self.pipe.__enter__()
            return self

        def __exit__(self, *exc):
            return self.pipe.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def watch(self, *keys):
            self.pipe.watch(*keys)
            if left[0] and key in keys:
                left[0] -= 1
                store.redis.hset(key, "racer", "x")

    monkeypatch.setattr(store.redis, "pipeline", Racing)


@pytest.mark.parametrize("part", ["ops", "pending"])
def test_a_control_whose_records_keep_changing_queues_nothing(store, monkeypatch, part):
    agent = kube(store)
    racing(monkeypatch, store, part, lifecycle.WRITE_ATTEMPTS)
    assert refused(store, agent, "drain") == (503, "lifecycle controls kept changing; nothing was queued")
    assert queued(store) == []


def test_one_racing_write_is_retried_and_the_control_queued(store, monkeypatch):
    agent = kube(store)
    racing(monkeypatch, store, "ops", 1)
    assert ask(store, agent, "drain")["state"] == "queued"
    assert len(queued(store)) == 1


def test_settling_clears_the_agents_pending_entry(store, ran):
    agent = local(store)
    pending = store.key(SLUG, "lifecycle-pending")
    ask(store, agent, "force_stop")
    assert store.redis.hget(pending, agent.name) == "op-first-1"
    command_runner.consume(store, SLUG)
    assert store.redis.hget(pending, agent.name) is None


def test_settling_retries_after_one_racing_write(store, monkeypatch):
    agent = local(store)
    record = ask(store, agent, "force_stop")
    racing(monkeypatch, store, "pending", 1)
    assert lifecycle.execute(store, SLUG, {"argv": ["force_stop", agent.name, record["op"]]}, lambda argv: "") == ""
    monkeypatch.undo()
    assert lifecycle.operations(store, SLUG)[0]["state"] == "applied"


def test_the_runner_fences_at_the_exact_expiry_and_on_a_gone_or_finished_agent(store):
    agent = kube(store)
    record = {"control": "drain", "at": 1_000, **lifecycle.seen(agent, {})}
    expired = 1_000 + lifecycle.PENDING_MS
    assert lifecycle._fenced(agent, record, True, expired) == "the control expired before the controller ran it"
    assert lifecycle._fenced(agent, record, True, expired - 1) == ""
    assert lifecycle._fenced(None, record, True, 1_000) == "the agent is no longer in this swarm"
    assert lifecycle._fenced(replace(agent, state="finished"), record, True, 1_000) == "the agent has finished"


def test_pending_ends_at_the_exact_expiry_and_hold(store, ran):
    agent = kube(store)
    ask(store, agent, "drain")
    at = json.loads(store.redis.hget(store.key(SLUG, "lifecycle-ops"), "op-first-1"))["at"]

    def pending(now):
        return lifecycle._pending(store, SLUG, store.redis, agent.name, now)

    assert (pending(at + lifecycle.PENDING_MS - 1), pending(at + lifecycle.PENDING_MS)) == ("op-first-1", "")
    command_runner.consume(store, SLUG)
    hold = at + lifecycle.WORKER_HOLD_MS
    assert (pending(hold - 1), pending(hold)) == ("op-first-1", "")


def test_admission_counts_a_finished_agent_as_stale(store, monkeypatch):
    agent = kube(store)
    monkeypatch.setattr(store, "agents", lambda slug: [replace(agent, state="finished")])
    assert refused(store, agent, "drain") == (409, STALE)
    assert lifecycle.conflicts(store, SLUG) == {"stale": 1}


def test_the_recorded_control_carries_its_fingerprint_and_no_error(store):
    agent = kube(store)
    assert ask(store, agent, "drain")["error"] == ""
    saved = json.loads(store.redis.hget(store.key(SLUG, "lifecycle-ops"), "op-first-1"))
    shown = {"control": "drain", "agent": agent.name, **lifecycle.seen(agent, {})}
    assert saved["fingerprint"] == lifecycle._digest(shown)
    assert lifecycle._digest({"b": 1, "a": 2}) == lifecycle._digest({"a": 2, "b": 1})


def test_identity_offers_and_support_hold_for_odd_agents(store):
    remote, other, near = kube(store), kube(store, "eng-3@life", "t3"), local(store)
    assert len(lifecycle.incarnation(remote)) == 16
    assert lifecycle.incarnation(remote) != lifecycle.incarnation(other)
    assert lifecycle.disabled(replace(remote, runtime_backend="ssh"), "attach", "", True) == "a ssh agent has no attach"
    assert lifecycle.disabled(replace(near, execution_id=""), "force_stop", "", True) == ""
    assert lifecycle.seen(replace(remote, task=""), {"": 9})["task_revision"] == 0
    unresolved = "an earlier control on this agent is still unresolved"
    assert lifecycle.offer(remote, {}, "op-x", True)["disabled"]["drain"] == unresolved


def test_operations_list_every_recent_control_newest_first(store):
    agents = [kube(store, f"eng-{n}@life", f"t{n}") for n in (1, 3, 4)]
    for n, agent in enumerate(agents):
        ask(store, agent, "drain", op=f"op-drain-{n}")
    assert [o["op"] for o in lifecycle.operations(store, SLUG)] == ["op-drain-2", "op-drain-1", "op-drain-0"]


def test_the_runner_runs_lifecycle_commands_with_the_page_environment(store, monkeypatch):
    envs = []
    monkeypatch.setattr(command_runner, "run", lambda argv, env: envs.append(env) or "")
    ask(store, local(store), "force_stop")
    command_runner.consume(store, SLUG)
    assert [env["AGENTIHOOKS_CONTROL_SOURCE"] for env in envs] == ["page", "page"]


def test_the_ledger_server_reads_the_stamps_of_its_own_ledger(store, monkeypatch):
    from scripts.swarm.store import SwarmError

    first, second = kube(store), kube(store, "eng-3@life", "t3")
    monkeypatch.setattr(ledger_server, "swarm_store", lambda: store)
    monkeypatch.setattr(ledger_server, "swarm_status", lambda slug: {"swarm": slug})
    for agent, stored in ((first, {}), (second, {"_meta": {}})):
        monkeypatch.setattr(
            ledger_server.repository, "read", lambda slug, *keys, stored=stored: {(SLUG, STAMPS): stored}[(slug, keys)]
        )
        sent = body(agent, "drain", op=f"op-drain-{agent.task}")
        assert ledger_server.lifecycle_control(SLUG, sent, True) == ({"swarm": SLUG}, "")

    def gone(*args, **kwargs):
        raise SwarmError("the swarm is gone")

    monkeypatch.setattr(lifecycle, "request", gone)
    assert ledger_server.lifecycle_control(SLUG, body(first, "drain"), True) == (None, "the swarm is gone")


def put_swarm(monkeypatch, sent, outcome):
    raw, calls, replies = json.dumps(sent).encode(), [], []

    def control(slug, given, operator):
        calls.append((slug, given, operator))
        return outcome()

    monkeypatch.setattr(ledger_server, "lifecycle_control", control)
    handler = SimpleNamespace(
        exists=lambda slug: True,
        refused=lambda slug: False,
        principal=None,
        headers={"Content-Length": str(len(raw))},
        rfile=io.BytesIO(raw),
        send=lambda *args: replies.append(args),
    )
    ledger_server.Handler.put_swarm(handler, SLUG)
    return calls, replies


def test_the_swarm_route_hands_a_lifecycle_body_to_its_control(monkeypatch):
    sent = {"action": "lifecycle", "control": "drain"}
    calls, replies = put_swarm(monkeypatch, sent, lambda: ({"swarm": SLUG}, ""))
    assert calls == [(SLUG, sent, True)]
    assert replies == [(200, json.dumps({"swarm": SLUG}), "application/json")]

    def stale():
        raise lifecycle.Refused(409, STALE)

    assert put_swarm(monkeypatch, sent, stale)[1] == [(409, STALE, "text/plain")]


def test_the_swarm_route_stops_at_a_refused_request(monkeypatch):
    replies = []
    monkeypatch.setattr(
        ledger_server, "lifecycle_control", lambda *args: pytest.fail("refused request reached control")
    )
    handler = SimpleNamespace(
        exists=lambda slug: True, refused=lambda slug: True, send=lambda *args: replies.append(args)
    )
    assert ledger_server.Handler.put_swarm(handler, SLUG) is None
    assert replies == []
