from dataclasses import replace

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
    projection,
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


def judge(agent, *signals, prior=None, limits=LIMITS, now=NOW):
    return classify(agent.execution_id, agent.generation, signals, prior, limits, now)


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
    assert seen.observed_at == NOW - 2.0
    assert seen.terminal is Terminal.UNOBSERVED
    assert observer.execution_observation_age_seconds("fixture", NOW) == {agent.execution_id: 2.0}


def test_fresh_heartbeat_alone_is_partial(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), ssh(agent))
    assert (seen.state, seen.terminal, seen.failure, seen.confidence) == (
        State.WORKING,
        Terminal.REACHABLE,
        Failure.NONE,
        Confidence.PARTIAL,
    )


def test_supervisor_handshake_confirms_a_fresh_heartbeat(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), signal(agent, Source.SUPERVISOR, value="confirmed"))
    assert seen.confidence is Confidence.CONFIRMED


def test_heartbeat_older_than_the_threshold_is_stale(fixture):
    _, agent, _ = fixture
    assert judge(agent, beat(agent, 120.0), pod(agent)).state is State.WORKING
    stale = judge(agent, beat(agent, 120.5), pod(agent))
    assert (stale.state, stale.failure, stale.confidence) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN)
    assert stale.suspect_since == NOW


def test_stale_heartbeat_with_running_pod_never_becomes_lost_without_proof(fixture):
    _, agent, _ = fixture
    first = judge(agent, beat(agent, 900.0), pod(agent))
    later = judge(agent, beat(agent, 900.0 + 3600), pod(agent), prior=first, now=NOW + 3600)
    assert later.state is State.SUSPECT
    assert later.suspect_since == NOW


def test_provider_wait_is_its_own_class(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent), signal(agent, Source.PROVIDER, value="quota_wait"))
    assert (seen.state, seen.failure) == (State.WAITING_QUOTA, Failure.PROVIDER_WAIT)


def test_unschedulable_pod_is_pending_scheduling(fixture):
    _, agent, _ = fixture
    seen = judge(agent, pod(agent, value="Pending"))
    assert (seen.state, seen.failure, seen.confidence) == (State.PENDING, Failure.POD_SCHEDULING, Confidence.PARTIAL)


def test_running_pod_before_its_first_heartbeat_is_starting(fixture):
    _, agent, _ = fixture
    seen = judge(agent, pod(agent))
    assert (seen.state, seen.failure) == (State.STARTING, Failure.NONE)
    assert judge(agent, pod(agent), signal(agent, Source.SUPERVISOR, value="confirmed")).state is State.SUSPECT


@pytest.mark.parametrize("reading", [Reading.FORBIDDEN, Reading.UNAUTHENTICATED])
def test_denied_pod_read_is_never_a_deleted_pod(fixture, reading):
    _, agent, _ = fixture
    first = judge(agent, beat(agent, 900.0), pod(agent, reading, ""))
    later = judge(agent, beat(agent, 900.0 + 3600), pod(agent, reading, ""), prior=first, now=NOW + 3600)
    for seen in (first, later):
        assert (seen.state, seen.failure, seen.confidence) == (
            State.SUSPECT,
            Failure.OBSERVATION_DENIED,
            Confidence.UNCERTAIN,
        )
        assert seen.denied == ("kubernetes",)


def test_denied_pod_read_beside_a_fresh_heartbeat_stays_working_and_names_the_denial(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent, Reading.FORBIDDEN, ""))
    assert (seen.state, seen.failure, seen.confidence, seen.denied) == (
        State.WORKING,
        Failure.OBSERVATION_DENIED,
        Confidence.PARTIAL,
        ("kubernetes",),
    )


@pytest.mark.parametrize("missing", [(), (Reading.UNREACHABLE,)])
def test_unreachable_or_missing_pod_view_is_unavailable(fixture, missing):
    _, agent, _ = fixture
    pods = [pod(agent, reading, "") for reading in missing]
    seen = judge(agent, beat(agent, 900.0), *pods)
    assert (seen.state, seen.failure, seen.confidence) == (
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
    assert (first.state, first.failure, first.confidence) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.PARTIAL)
    early = judge(agent, proof(agent), prior=first, now=NOW + 599.0)
    assert early.state is State.SUSPECT
    lost = judge(agent, proof(agent), prior=early, now=NOW + 600.0)
    assert (lost.state, lost.suspect_since, lost.needs_operator) == (State.LOST, NOW, False)
    assert judge(agent, beat(agent), pod(agent), prior=lost, now=NOW + 601.0).state is State.LOST


def test_lost_needs_a_prior_suspect_observation(fixture):
    _, agent, _ = fixture
    prior = judge(agent, beat(agent), pod(agent))
    assert judge(agent, pod(agent, Reading.NOT_FOUND, ""), prior=prior, now=NOW + 5000).state is State.SUSPECT


def test_conservative_mode_never_declares_lost_and_asks_the_operator(fixture):
    _, agent, _ = fixture
    limits = replace(LIMITS, mode=Mode.CONSERVATIVE)
    first = judge(agent, pod(agent, Reading.NOT_FOUND, ""), limits=limits)
    later = judge(agent, pod(agent, Reading.NOT_FOUND, ""), prior=first, limits=limits, now=NOW + 5000)
    assert (later.state, later.needs_operator) == (State.SUSPECT, True)
    assert judge(agent, beat(agent), limits=limits).needs_operator is False


def test_fresh_heartbeat_contradicted_by_a_deleted_pod_is_suspect(fixture):
    _, agent, _ = fixture
    seen = judge(agent, beat(agent), pod(agent, Reading.NOT_FOUND, ""))
    assert (seen.state, seen.failure, seen.confidence) == (State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN)


def test_relist_after_watch_failure_recovers_without_a_new_agent(fixture):
    store, agent, observer = fixture
    lost_watch = observer.observe("fixture", agent, [beat(agent, 300.0), pod(agent, Reading.UNREACHABLE, "")], NOW)
    assert lost_watch.state is State.SUSPECT
    relisted = [beat(agent, -10.0), pod(agent, age=-10.0), signal(agent, Source.SUPERVISOR, value="confirmed")]
    seen = observer.observe("fixture", agent, relisted, NOW + 10)
    assert (seen.state, seen.confidence, seen.recovered, seen.suspect_since) == (
        State.WORKING,
        Confidence.CONFIRMED,
        True,
        0.0,
    )
    assert [a.execution_id for a in store.agents("fixture")] == [agent.execution_id]


def test_an_older_observation_cannot_overwrite_a_newer_one(fixture):
    _, agent, observer = fixture
    newer = observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    replayed = observer.observe("fixture", agent, [beat(agent, 900.0), pod(agent, Reading.UNREACHABLE, "", 900.0)], NOW)
    assert replayed == newer
    assert observer.get("fixture", agent.execution_id) == newer


def test_signals_for_another_execution_or_generation_are_discarded(fixture):
    _, agent, observer = fixture
    foreign = [
        replace(pod(agent, Reading.NOT_FOUND, ""), execution_id="other"),
        signal(agent, Source.KUBERNETES, Reading.NOT_FOUND, generation=agent.generation + 1),
    ]
    seen = observer.observe("fixture", agent, [beat(agent), *foreign], NOW)
    assert (seen.state, set(seen.sources)) == (State.WORKING, {"heartbeat"})
    assert observer.discarded == 2


def test_observer_refuses_another_backends_execution(fixture):
    store, agent, _ = fixture
    local = Observer(store, "local", LIMITS)
    with pytest.raises(ObservationRefused, match="belongs to kubernetes"):
        local.observe("fixture", agent, [beat(agent)], NOW)
    assert local.get("fixture", agent.execution_id) is None


@pytest.mark.parametrize("change", [{"execution_id": ""}, {"generation": 99}])
def test_observer_refuses_an_execution_that_is_not_the_seat_occupant(fixture, change):
    _, agent, observer = fixture
    with pytest.raises(ObservationRefused):
        observer.observe("fixture", replace(agent, **change), [beat(agent)], NOW)
    assert observer.records("fixture") == []


def test_mode_comes_from_the_environment():
    assert Thresholds.from_environ({}).mode is Mode.STANDARD
    assert Thresholds.from_environ({"AGENTIHOOKS_OBSERVATION_MODE": "conservative"}).mode is Mode.CONSERVATIVE


def test_projection_carries_source_and_time_for_recorded_and_legacy_agents(fixture):
    from scripts.swarm import idle

    store, agent, observer = fixture
    seen = observer.observe("fixture", agent, [beat(agent), pod(agent)], NOW)
    assert projection(store, "fixture", agent) == {
        "state": "working",
        "terminal": "unobserved",
        "failure": "none",
        "confidence": "confirmed",
        "observed_at": seen.observed_at,
        "sources": seen.sources,
    }
    legacy = AgentRecord("sw-eng-1", "eng", "t1")
    assert projection(store, "fixture", legacy) == {"state": "unclassified", "observed_at": 0.0, "sources": {}}
    idle.beat(store.redis, "fixture", "sw-eng-1", "working", 1_500)
    assert projection(store, "fixture", legacy) == {
        "state": "unclassified",
        "observed_at": 1.5,
        "sources": {"heartbeat": {"reading": "ok", "observed_at": 1.5, "value": "working"}},
    }


def test_status_report_rows_carry_the_observation(monkeypatch):
    from scripts.swarm import status

    store, agent = fresh_store()
    Observer(store, "kubernetes", LIMITS).observe("fixture", agent, [beat(agent)], NOW)
    monkeypatch.setattr(status, "page_quota", lambda: {})
    rows = status.status_report(store, "fixture", {"tasks": []})["agents"]
    assert [(row["name"], row["observation"]["sources"]["heartbeat"]["observed_at"]) for row in rows] == [
        (agent.name, NOW - 5.0)
    ]


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
    with pytest.raises(ObservationRefused, match="kept changing"):
        observer.observe("fixture", agent, [beat(agent)], NOW)
    assert observer.records("fixture") == []
