import json
from dataclasses import asdict, replace

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.observe import (
    Confidence,
    Failure,
    Mode,
    ObservationRefused,
    Observer,
    Reading,
    Signal,
    Source,
    State,
    Terminal,
    Thresholds,
    classify,
    rule,
)

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
NOW = 1_800_000_000.0
LIMITS = Thresholds(fresh_s=120.0, lost_after_s=600.0)


def fresh_store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": "workers", "pod_name": "attempt"},
        ),
    )
    return store, agent


@pytest.fixture
def fixture():
    store, agent = fresh_store()
    return store, agent, Observer(store, "kubernetes", LIMITS)


def signal(agent, source, reading=Reading.OK, value="", age=0.0, generation=None):
    return Signal(
        source,
        reading,
        NOW - age,
        agent.execution_id,
        agent.generation if generation is None else generation,
        value,
    )


def beat(agent, age=5.0):
    return signal(agent, Source.HEARTBEAT, value="working", age=age)


def pod(agent, reading=Reading.OK, value="Running", age=1.0):
    return signal(agent, Source.KUBERNETES, reading, value, age)


def ssh(agent, reading=Reading.OK, age=1.0):
    return signal(agent, Source.TERMINAL, reading, age=age)


def handshake(agent, age=1.0):
    return signal(agent, Source.SUPERVISOR, value="confirmed", age=age)


def judge(agent, *signals, prior=None, limits=LIMITS, now=NOW):
    return classify(agent.execution_id, agent.generation, signals, prior, limits, now)


def verdict(seen):
    return seen.state, seen.failure, seen.confidence


@pytest.mark.parametrize("independent", range(2))
def test_terminal_loss_with_fresh_heartbeat_keeps_the_task_working(fixture, independent):
    store, agent, observer = fixture
    seen = observer.observe("fixture", agent, [beat(agent), pod(agent), ssh(agent, Reading.UNREACHABLE)], NOW)
    assert (seen.state, seen.terminal, seen.failure) == (State.WORKING, Terminal.DEGRADED, Failure.TERMINAL_LOSS)
    assert seen.confidence is Confidence.CONFIRMED
    assert observer.get("fixture", agent.execution_id) == seen
    assert store.execution("fixture", agent.execution_id).generation == agent.generation


def test_every_source_keeps_its_own_reading_and_time(fixture):
    _, agent, observer = fixture
    seen = observer.observe("fixture", agent, [beat(agent, 30.0), pod(agent, age=2.0)], NOW)
    assert seen.sources == {
        "heartbeat": {"reading": "ok", "observed_at": NOW - 30.0, "value": "working"},
        "kubernetes": {"reading": "ok", "observed_at": NOW - 2.0, "value": "Running"},
    }
    assert (seen.observed_at, seen.confirmed_at, seen.classified_at) == (NOW - 2.0, NOW - 2.0, NOW)
    assert seen.terminal is Terminal.UNOBSERVED
    assert observer.execution_observation_age_seconds("fixture", NOW) == {agent.execution_id: 2.0}


def test_age_follows_the_last_successful_read_not_failed_ones(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [beat(agent, 30.0)], NOW)
    for minute in range(1, 4):
        later = pod(agent, Reading.FORBIDDEN, "", age=-60.0 * minute)
        seen = observer.observe("fixture", agent, [later], NOW + 60 * minute)
    assert (seen.observed_at, seen.confirmed_at) == (NOW + 180, NOW - 30.0)
    assert observer.execution_observation_age_seconds("fixture", NOW + 180) == {agent.execution_id: 210.0}


def test_age_is_unknown_before_any_successful_read(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [pod(agent, Reading.FORBIDDEN, "")], NOW)
    assert observer.execution_observation_age_seconds("fixture", NOW) == {agent.execution_id: None}


def test_fresh_heartbeat_alone_is_partial(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), ssh(agent))
    assert (seen.terminal, *verdict(seen)) == (Terminal.REACHABLE, State.WORKING, Failure.NONE, Confidence.PARTIAL)


def test_supervisor_handshake_confirms_a_fresh_heartbeat(fixture):
    _, agent, _ = fixture
    assert judge(agent, beat(agent), handshake(agent)).confidence is Confidence.CONFIRMED


def test_an_old_remembered_pod_reading_no_longer_confirms(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [beat(agent), pod(agent), handshake(agent)], NOW)
    seen = observer.observe("fixture", agent, [beat(agent, -3600.0)], NOW + 3600)
    assert (seen.state, seen.confidence) == (State.WORKING, Confidence.PARTIAL)
    assert seen.sources["kubernetes"]["observed_at"] == NOW - 1.0


def test_heartbeat_older_than_the_threshold_is_stale(fixture):
    _, agent, _ = fixture
    assert judge(agent, beat(agent, 120.0), pod(agent)).state is State.WORKING
    stale = judge(agent, beat(agent, 120.5), pod(agent))
    assert verdict(stale) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN)
    assert (stale.suspect_since, stale.proof_since) == (NOW, 0.0)


def test_stale_heartbeat_with_running_pod_never_becomes_lost_without_proof(fixture):
    _, agent, _ = fixture
    first = judge(agent, beat(agent, 900.0), pod(agent))
    later = judge(agent, prior=first, now=NOW + 3600)
    assert (later.state, later.suspect_since) == (State.SUSPECT, NOW)


def test_an_empty_batch_reclassifies_known_evidence_as_time_passes(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    seen = observer.observe("fixture", agent, [], NOW + 600)
    assert (seen.state, seen.classified_at, seen.confirmed_at) == (State.SUSPECT, NOW + 600, NOW - 1.0)


def test_provider_wait_is_its_own_class(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent), signal(agent, Source.PROVIDER, value="quota_wait"))
    assert (seen.state, seen.failure) == (State.WAITING_QUOTA, Failure.PROVIDER_WAIT)


def test_unschedulable_pod_is_pending_scheduling(fixture):
    _, agent, _ = fixture
    assert verdict(judge(agent, pod(agent, value="Pending"))) == (
        State.PENDING,
        Failure.POD_SCHEDULING,
        Confidence.PARTIAL,
    )


def test_fresh_heartbeat_from_a_pending_pod_is_a_contradiction(fixture):
    _, agent, _ = fixture
    assert verdict(judge(agent, beat(agent), pod(agent, value="Pending"))) == (
        State.SUSPECT,
        Failure.POD_SCHEDULING,
        Confidence.UNCERTAIN,
    )


def test_running_pod_is_starting_until_the_supervisor_confirms_the_agent(fixture):
    _, agent, _ = fixture
    assert verdict(judge(agent, pod(agent))) == (State.STARTING, Failure.NONE, Confidence.PARTIAL)
    assert verdict(judge(agent, pod(agent), handshake(agent))) == (State.WORKING, Failure.NONE, Confidence.PARTIAL)


@pytest.mark.parametrize("reading", [Reading.FORBIDDEN, Reading.UNAUTHENTICATED])
def test_denied_pod_read_is_never_a_deleted_pod(fixture, reading):
    _, agent, _ = fixture
    first = judge(agent, beat(agent, 900.0), pod(agent, reading, ""))
    later = judge(agent, pod(agent, reading, "", age=-3600.0), prior=first, now=NOW + 3600)
    for seen in (first, later):
        assert verdict(seen) == (State.SUSPECT, Failure.OBSERVATION_DENIED, Confidence.UNCERTAIN)
        assert seen.denied == ("kubernetes",)


def test_denied_pod_read_beside_a_fresh_heartbeat_stays_working_and_names_the_denial(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent, Reading.FORBIDDEN, ""))
    assert (*verdict(seen), seen.denied) == (
        State.WORKING,
        Failure.OBSERVATION_DENIED,
        Confidence.PARTIAL,
        ("kubernetes",),
    )


@pytest.mark.parametrize("missing", [(), (Reading.UNREACHABLE,)])
def test_unreachable_or_missing_pod_view_is_unavailable(fixture, missing):
    _, agent, _ = fixture
    pods = [pod(agent, reading, "") for reading in missing]
    assert verdict(judge(agent, beat(agent, 900.0), *pods)) == (
        State.SUSPECT,
        Failure.OBSERVATION_UNAVAILABLE,
        Confidence.UNCERTAIN,
    )


@pytest.mark.parametrize(
    "proof",
    [
        lambda agent: pod(agent, Reading.NOT_FOUND, ""),
        lambda agent: pod(agent, value="Failed"),
        lambda agent: pod(agent, value="Succeeded"),
        lambda agent: signal(agent, Source.SUPERVISOR, value="exited"),
    ],
)
def test_worker_loss_proof_turns_suspect_into_lost_after_the_threshold(fixture, proof):
    _, agent, _ = fixture
    first = judge(agent, beat(agent, 900.0), proof(agent))
    assert verdict(first) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.PARTIAL)
    assert first.proof_since == NOW
    early = judge(agent, prior=first, now=NOW + 599.0)
    assert (early.state, early.proof_since) == (State.SUSPECT, NOW)
    lost = judge(agent, prior=early, now=NOW + 600.0)
    assert (lost.state, lost.suspect_since, lost.needs_operator) == (State.LOST, NOW, False)
    assert judge(agent, beat(agent, -601.0), pod(agent, age=-601.0), prior=lost, now=NOW + 601.0) == lost


def test_the_lost_clock_starts_at_the_first_proof_not_at_an_earlier_suspicion(fixture):
    _, agent, _ = fixture
    denied = judge(agent, pod(agent, Reading.FORBIDDEN, ""))
    proof = judge(agent, pod(agent, Reading.NOT_FOUND, "", age=-602.0), prior=denied, now=NOW + 602)
    assert (proof.state, proof.suspect_since, proof.proof_since) == (State.SUSPECT, NOW, NOW + 602)


def test_lost_needs_a_prior_proof_observation(fixture):
    _, agent, _ = fixture
    prior = judge(agent, beat(agent), pod(agent))
    gone = judge(agent, pod(agent, Reading.NOT_FOUND, ""), prior=prior, now=NOW + 5000)
    assert (*verdict(gone), gone.proof_since) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.PARTIAL, NOW + 5000)


def test_conservative_mode_never_declares_lost_and_asks_the_operator(fixture):
    _, agent, _ = fixture
    limits = replace(LIMITS, mode=Mode.CONSERVATIVE)
    first = judge(agent, pod(agent, Reading.NOT_FOUND, ""), limits=limits)
    later = judge(agent, prior=first, limits=limits, now=NOW + 5000)
    assert (later.state, later.needs_operator) == (State.SUSPECT, True)
    assert judge(agent, beat(agent), limits=limits).needs_operator is False


def test_fresh_heartbeat_contradicted_by_a_deleted_pod_is_suspect(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent, Reading.NOT_FOUND, ""))
    assert verdict(seen) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN)


def test_relist_after_watch_failure_recovers_without_a_new_agent(fixture):
    store, agent, observer = fixture
    lost_watch = observer.observe("fixture", agent, [beat(agent, 300.0), pod(agent, Reading.UNREACHABLE, "")], NOW)
    assert lost_watch.state is State.SUSPECT
    relisted = [beat(agent, -10.0), pod(agent, age=-10.0), handshake(agent, -10.0)]
    seen = observer.observe("fixture", agent, relisted, NOW + 10)
    assert (seen.state, seen.confidence, seen.recovered, seen.suspect_since) == (
        State.WORKING,
        Confidence.CONFIRMED,
        True,
        0.0,
    )
    assert [a.execution_id for a in store.agents("fixture")] == [agent.execution_id]


def test_older_signals_cannot_overwrite_newer_evidence(fixture):
    _, agent, observer = fixture
    newer = observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    replayed = observer.observe("fixture", agent, [beat(agent, 900.0), pod(agent, Reading.UNREACHABLE, "", 900.0)], NOW)
    assert replayed == newer
    assert observer.get("fixture", agent.execution_id) == newer


def test_an_earlier_classification_cannot_overwrite_a_later_one(fixture):
    _, agent, observer = fixture
    later = observer.observe("fixture", agent, [beat(agent)], NOW + 60)
    assert observer.observe("fixture", agent, [pod(agent, Reading.NOT_FOUND, "", age=-90.0)], NOW) == later
    assert observer.get("fixture", agent.execution_id) == later


def test_signals_for_another_execution_or_generation_are_discarded(fixture):
    _, agent, observer = fixture
    foreign = [
        replace(pod(agent, Reading.NOT_FOUND, ""), execution_id="other"),
        signal(agent, Source.KUBERNETES, Reading.NOT_FOUND, generation=agent.generation + 1),
    ]
    seen = observer.observe("fixture", agent, [beat(agent), *foreign], NOW)
    assert (seen.state, set(seen.sources)) == (State.WORKING, {"heartbeat"})
    assert observer.discarded == 2


def test_lost_writes_one_audit_record(fixture):
    _, agent, observer = fixture
    gone = [pod(agent, Reading.NOT_FOUND, "")]
    observer.observe("fixture", agent, gone, NOW)
    assert observer.audit("fixture") == []
    lost = observer.observe("fixture", agent, [], NOW + 600)
    observer.observe("fixture", agent, [], NOW + 700)
    assert lost.state is State.LOST
    assert observer.audit("fixture") == [lost]


def test_observer_refuses_another_backends_execution(fixture):
    store, agent, _ = fixture
    local = Observer(store, "local", LIMITS)
    with pytest.raises(ObservationRefused, match="belongs to kubernetes") as refused:
        local.observe("fixture", agent, [beat(agent)], NOW)
    assert (refused.value.error_class, refused.value.retryable) == ("forbidden_scope", False)
    assert local.get("fixture", agent.execution_id) is None


@pytest.mark.parametrize("change", [{"execution_id": ""}, {"generation": 99}])
def test_observer_refuses_an_execution_that_is_not_the_seat_occupant(fixture, change):
    _, agent, observer = fixture
    with pytest.raises(ObservationRefused, match="current occupant") as refused:
        observer.observe("fixture", replace(agent, **change), [beat(agent)], NOW)
    assert (refused.value.error_class, refused.value.retryable) == ("stale_generation", False)
    assert observer.records("fixture") == []


def test_mode_comes_from_the_environment():
    assert Thresholds.from_environ({}).mode is Mode.STANDARD
    assert Thresholds.from_environ({"AGENTIHOOKS_OBSERVATION_MODE": "conservative"}).mode is Mode.CONSERVATIVE


def test_status_observation_carries_source_and_time_for_recorded_and_legacy_agents(fixture):
    from scripts.swarm import idle, status

    store, agent, observer = fixture
    seen = observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    assert status.observation(store, "fixture", agent) == {
        "state": "working",
        "terminal": "unobserved",
        "failure": "none",
        "confidence": "confirmed",
        "needs_operator": False,
        "observed_at": seen.observed_at,
        "confirmed_at": seen.confirmed_at,
        "sources": seen.sources,
    }
    legacy = AgentRecord("sw-eng-1", "eng", "t1")
    assert status.observation(store, "fixture", legacy) == {"state": "unclassified", "observed_at": None, "sources": {}}
    idle.beat(store.redis, "fixture", "sw-eng-1", "working", 1_500)
    assert status.observation(store, "fixture", legacy) == {
        "state": "unclassified",
        "observed_at": 1.5,
        "sources": {"heartbeat": {"reading": "ok", "observed_at": 1.5, "value": "working"}},
    }


def test_status_report_rows_keep_their_fields_and_add_the_observation(monkeypatch):
    from scripts.swarm import status

    store, agent = fresh_store()
    Observer(store, "kubernetes", LIMITS).observe("fixture", agent, [beat(agent)], NOW)
    monkeypatch.setattr(status, "page_quota", lambda: {})
    [row] = status.status_report(store, "fixture", {"tasks": []})["agents"]
    assert set(row) == set(asdict(agent)) | {"status", "promoted", "state_since", "gates", "inbox", "observation"}
    assert (row["name"], row["observation"]["sources"]["heartbeat"]["observed_at"]) == (agent.name, NOW - 5.0)


class Contended:
    def __init__(self, pipe, failures):
        self.pipe, self.failures = pipe, failures

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.pipe.reset()

    def __getattr__(self, name):
        return getattr(self.pipe, name)

    def execute(self):
        from redis.exceptions import WatchError

        if self.failures:
            self.failures.pop()
            raise WatchError("changed")
        return self.pipe.execute()


@pytest.mark.parametrize("failures", [1, 5])
def test_a_contended_record_retries_then_refuses(fixture, monkeypatch, failures):
    store, agent, observer = fixture
    real, left = store.redis.pipeline, [None] * failures
    monkeypatch.setattr(store.redis, "pipeline", lambda: Contended(real(), left))
    if failures < 5:
        assert observer.observe("fixture", agent, [beat(agent)], NOW).state is State.WORKING
        return
    with pytest.raises(ObservationRefused, match="kept changing") as refused:
        observer.observe("fixture", agent, [beat(agent)], NOW)
    assert (refused.value.error_class, refused.value.retryable) == ("revision_conflict", True)
    assert observer.records("fixture") == []


def test_a_reading_exactly_at_the_freshness_limit_still_confirms(fixture):
    _, agent, _ = fixture
    assert judge(agent, beat(agent), pod(agent, age=120.0)).confidence is Confidence.CONFIRMED


def test_no_evidence_has_no_observation_time(fixture):
    _, agent, _ = fixture
    seen = judge(agent)
    assert (seen.observed_at, seen.confirmed_at, seen.sources) == (0.0, 0.0, {})


def test_recovered_marks_only_a_suspect_to_alive_change(fixture):
    _, agent, _ = fixture
    working = judge(agent, beat(agent), pod(agent))
    stale = judge(agent, beat(agent, 900.0), pod(agent))
    still = judge(agent, prior=stale, now=NOW + 60)
    assert (working.recovered, stale.recovered, still.recovered) == (False, False, False)


def test_records_live_under_the_swarm_observation_keys(fixture):
    store, agent, observer = fixture
    observer.observe("fixture", agent, [pod(agent, Reading.NOT_FOUND, "")], NOW)
    observer.observe("fixture", agent, [], NOW + 600)
    assert store.redis.hexists(store.key("fixture", "observations"), agent.execution_id)
    assert store.redis.llen(store.key("fixture", "observation-audit")) == 1


def test_an_unoccupied_seat_is_refused(fixture):
    _, agent, observer = fixture
    with pytest.raises(ObservationRefused, match=r"^execution is not the current occupant of its seat$"):
        observer.observe("fixture", replace(agent, seat="eng-9@fixture"), [beat(agent)], NOW)


def test_discarded_signals_accumulate_across_observations(fixture):
    _, agent, observer = fixture
    foreign = replace(beat(agent), execution_id="other")
    for at in (NOW, NOW + 1):
        observer.observe("fixture", agent, [foreign, foreign], at)
    assert observer.discarded == 4


def test_audit_lists_every_lost_execution(fixture):
    store, agent, observer = fixture
    agents = [agent]
    for seat in ("eng-2@fixture", "eng-3@fixture"):
        record = replace(agent, name=store.next_name("fixture", "eng"), seat=seat, execution_id="", generation=0)
        agents.append(store.start_execution("fixture", record))
    for each in agents:
        observer.observe("fixture", each, [pod(each, Reading.NOT_FOUND, "")], NOW)
        observer.observe("fixture", each, [], NOW + 600)
    assert sorted(record.execution_id for record in observer.audit("fixture")) == sorted(
        each.execution_id for each in agents
    )


def test_a_classification_at_the_same_time_takes_new_evidence(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [beat(agent), ssh(agent)], NOW)
    seen = observer.observe("fixture", agent, [ssh(agent, Reading.UNREACHABLE, age=0.5)], NOW)
    assert (seen.terminal, seen.failure) == (Terminal.DEGRADED, Failure.TERMINAL_LOSS)


def test_the_audit_entry_is_written_at_the_lost_transition(fixture):
    _, agent, observer = fixture
    observer.observe("fixture", agent, [pod(agent, Reading.NOT_FOUND, "")], NOW)
    lost = observer.observe("fixture", agent, [], NOW + 600)
    assert observer.audit("fixture") == [lost]


def test_contention_refusal_names_its_failure(fixture, monkeypatch):
    store, agent, observer = fixture
    real, left = store.redis.pipeline, [None] * 5
    monkeypatch.setattr(store.redis, "pipeline", lambda: Contended(real(), left))
    with pytest.raises(ObservationRefused, match=r"^observation record kept changing; nothing recorded$"):
        observer.observe("fixture", agent, [beat(agent)], NOW)


CAUTIOUS = replace(LIMITS, mode=Mode.CONSERVATIVE)


@pytest.fixture
def cautious():
    store, agent = fresh_store()
    return store, agent, Observer(store, "kubernetes", CAUTIOUS)


def flagged(observer, agent, *signals):
    seen = observer.observe("fixture", agent, signals or [beat(agent, 900.0), pod(agent)], NOW)
    assert (seen.state, seen.needs_operator) == (State.SUSPECT, True)
    return seen


def test_a_lost_classification_is_recorded_with_its_reason_and_held(cautious):
    store, agent, observer = cautious
    flagged(observer, agent)
    ruled = rule(store, "fixture", agent.execution_id, "lost", " pod gone in the console ", "operator", NOW + 10)
    assert (ruled.state, ruled.needs_operator, ruled.classified_at) == (State.LOST, False, NOW + 10)
    assert (ruled.ruling, ruled.ruling_reason, ruled.ruled_by, ruled.ruled_at) == (
        "lost",
        "pod gone in the console",
        "operator",
        NOW + 10,
    )
    assert observer.get("fixture", agent.execution_id) == ruled
    assert observer.audit("fixture") == [ruled]
    alive = [beat(agent, -20.0), pod(agent, age=-20.0), handshake(agent, -20.0)]
    assert observer.observe("fixture", agent, alive, NOW + 20) == ruled


def test_a_working_classification_holds_the_attempt_working_on_the_same_evidence(cautious):
    store, agent, observer = cautious
    suspect = flagged(observer, agent)
    ruled = rule(store, "fixture", agent.execution_id, "working", "agent answered in its pane", "master@x-1", NOW + 10)
    assert (ruled.state, ruled.needs_operator, ruled.suspect_since, ruled.failure) == (
        State.WORKING,
        False,
        0.0,
        suspect.failure,
    )
    later = observer.observe("fixture", agent, [], NOW + 5000)
    assert (later.state, later.needs_operator, later.suspect_since) == (State.WORKING, False, 0.0)
    assert (later.ruling, later.ruling_reason, later.ruled_by, later.ruled_at) == (
        "working",
        "agent answered in its pane",
        "master@x-1",
        NOW + 10,
    )
    assert observer.audit("fixture") == []


def test_a_working_classification_also_covers_loss_proof_seen_before_it(cautious):
    store, agent, observer = cautious
    flagged(observer, agent, pod(agent, Reading.NOT_FOUND, ""))
    rule(store, "fixture", agent.execution_id, "working", "pod was recreated by hand", "operator", NOW + 10)
    later = observer.observe("fixture", agent, [], NOW + 5000)
    assert (later.state, later.needs_operator, later.proof_since) == (State.WORKING, False, NOW)


def test_new_loss_proof_after_a_working_classification_asks_the_operator_again(cautious):
    store, agent, observer = cautious
    flagged(observer, agent)
    rule(store, "fixture", agent.execution_id, "working", "agent answered in its pane", "operator", NOW + 10)
    gone = observer.observe("fixture", agent, [pod(agent, Reading.NOT_FOUND, "", age=-20.0)], NOW + 20)
    assert (gone.state, gone.needs_operator, gone.proof_since, gone.suspect_since) == (
        State.SUSPECT,
        True,
        NOW + 20,
        NOW + 20,
    )
    assert (gone.ruling, gone.ruling_reason, gone.ruled_by, gone.ruled_at) == ("", "", "", 0.0)


def test_a_working_classification_ends_when_the_attempt_recovers_on_its_own(cautious):
    store, agent, observer = cautious
    flagged(observer, agent)
    rule(store, "fixture", agent.execution_id, "working", "agent answered in its pane", "operator", NOW + 10)
    alive = observer.observe("fixture", agent, [beat(agent, -20.0), pod(agent, age=-20.0)], NOW + 20)
    assert (alive.state, alive.ruling, alive.ruled_at) == (State.WORKING, "", 0.0)
    again = observer.observe("fixture", agent, [], NOW + 2000)
    assert (again.state, again.needs_operator) == (State.SUSPECT, True)


@pytest.mark.parametrize(
    ("ruling", "reason", "message", "error_class"),
    [
        ("working", "  ", "needs a reason", "invalid_request"),
        ("gone", "why", "one of lost, working", "invalid_request"),
        ("suspect", "why", "one of lost, working", "invalid_request"),
    ],
)
def test_a_classification_refuses_a_bad_request(cautious, ruling, reason, message, error_class):
    store, agent, observer = cautious
    before = flagged(observer, agent)
    with pytest.raises(ObservationRefused, match=message) as refused:
        rule(store, "fixture", agent.execution_id, ruling, reason, "operator", NOW + 10)
    assert (refused.value.error_class, refused.value.retryable) == (error_class, False)
    assert observer.get("fixture", agent.execution_id) == before


def test_a_classification_refuses_an_attempt_that_does_not_need_the_operator(cautious):
    store, agent, observer = cautious
    with pytest.raises(ObservationRefused, match="does not need an operator classification") as unknown:
        rule(store, "fixture", agent.execution_id, "lost", "why", "operator", NOW)
    assert (unknown.value.error_class, unknown.value.retryable) == ("not_suspect", False)
    healthy = observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    with pytest.raises(ObservationRefused, match="does not need an operator classification"):
        rule(store, "fixture", agent.execution_id, "lost", "why", "operator", NOW + 10)
    assert observer.get("fixture", agent.execution_id) == healthy
    assert observer.audit("fixture") == []


def classify_cli(monkeypatch, store, *argv):
    from scripts.swarm import cli

    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli.time, "time", lambda: NOW + 10)
    return cli.main(["fixture", *argv])


@pytest.mark.parametrize("who", ["operator", "master"])
def test_the_master_or_the_operator_classifies_through_the_swarm_command(cautious, monkeypatch, capsys, who):
    store, agent, observer = cautious
    flagged(observer, agent)
    name = "operator"
    if who == "master":
        name = store.next_name("fixture", "master")
        store.put_agent("fixture", AgentRecord(name, "master", ""))
    args = ["--as", name, "classify", agent.execution_id, "lost", "--reason", "pod gone"]
    assert classify_cli(monkeypatch, store, *args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "execution_id": agent.execution_id,
        "state": "lost",
        "ruling": "lost",
        "reason": "pod gone",
        "by": name,
    }
    ruled = observer.get("fixture", agent.execution_id)
    assert (ruled.state, ruled.ruled_by, ruled.ruled_at) == (State.LOST, name, NOW + 10)


def test_a_lane_agent_may_not_classify_an_attempt(cautious, monkeypatch, capsys):
    store, agent, observer = cautious
    before = flagged(observer, agent)
    assert agent.name in [found.name for found in store.agents("fixture")]
    args = ["--as", agent.name, "classify", agent.execution_id, "working", "--reason", "fine"]
    assert classify_cli(monkeypatch, store, *args) == 1
    assert "only the master or the operator classifies an execution attempt" in capsys.readouterr().err
    assert observer.get("fixture", agent.execution_id) == before


def test_a_refused_classification_is_reported_by_the_swarm_command(cautious, monkeypatch, capsys):
    store, agent, _ = cautious
    args = ["--as", "operator", "classify", agent.execution_id, "lost", "--reason", "gone"]
    assert classify_cli(monkeypatch, store, *args) == 1
    assert "does not need an operator classification" in capsys.readouterr().err
