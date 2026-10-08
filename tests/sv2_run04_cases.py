from dataclasses import replace

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.runtime.observe import (
    Failure,
    Mode,
    ObservationRefused,
    Observer,
    Reading,
    Source,
    State,
    Terminal,
)
from tests.test_swarm_v2_observe import LIMITS, NOW, beat, fresh_store, pod, signal, ssh

OBSERVATIONS = "observations"


def _protected(store):
    return {key: entry for key, entry in store.export("fixture")["keys"].items() if not key.endswith(OBSERVATIONS)}


def _refused_elsewhere(store, agent):
    try:
        Observer(store, "local", LIMITS).observe("fixture", agent, [beat(agent)], NOW)
    except ObservationRefused:
        return True
    return False


def case_a():
    runs = []
    for _ in range(2):
        store, agent = fresh_store()
        observer = Observer(store, "kubernetes", LIMITS)
        seen = observer.observe("fixture", agent, [beat(agent), pod(agent), ssh(agent, Reading.UNREACHABLE)], NOW)
        stored = observer.get("fixture", agent.execution_id)
        runs.append(
            {
                "passed": seen == stored
                and (stored.state, stored.terminal, stored.failure)
                == (State.WORKING, Terminal.DEGRADED, Failure.TERMINAL_LOSS)
                and store.execution("fixture", agent.execution_id).generation == agent.generation
                and _refused_elsewhere(store, agent),
                "accepted": {"state": stored.state, "terminal": stored.terminal, "failure": stored.failure},
                "confidence": stored.confidence,
                "sources": sorted(stored.sources),
                "other_backend_refused": _refused_elsewhere(store, agent),
                "execution_observation_age_seconds": observer.execution_observation_age_seconds("fixture", NOW),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_b():
    runs = []
    for _ in range(2):
        store, agent = fresh_store()
        observer = Observer(store, "kubernetes", LIMITS)
        before = _protected(store)
        denied = []
        for offset, reading in ((0.0, Reading.FORBIDDEN), (7200.0, Reading.UNAUTHENTICATED)):
            first = [beat(agent, 900.0 - offset), pod(agent, reading, "", age=-offset)]
            denied.append(observer.observe("fixture", agent, first, NOW + offset))
            later = [pod(agent, reading, "", age=-offset - 3600.0)]
            denied.append(observer.observe("fixture", agent, later, NOW + offset + 3600))
        unchanged = before == _protected(store)
        corrected = observer.observe("fixture", agent, [beat(agent, -14400.0), pod(agent, age=-14400.0)], NOW + 14400)
        runs.append(
            {
                "passed": all(
                    seen.state is State.SUSPECT
                    and seen.failure is Failure.OBSERVATION_DENIED
                    and seen.denied == (Source.KUBERNETES.value,)
                    for seen in denied
                )
                and unchanged
                and corrected.state is State.WORKING,
                "classified": [{"state": seen.state, "failure": seen.failure} for seen in denied],
                "deleted_pod_claimed": any(seen.state is State.LOST for seen in denied),
                "protected_state_unchanged": unchanged,
                "corrected_by_new_read": corrected.state,
                "execution_observation_age_seconds": observer.execution_observation_age_seconds("fixture", NOW + 3700),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_c():
    import fakeredis

    runs = []
    for _ in range(2):
        store, agent = fresh_store()
        observer = Observer(store, "kubernetes", LIMITS)
        stale = observer.observe("fixture", agent, [beat(agent, 900.0), pod(agent)], NOW)
        watch_lost = [beat(agent, 900.0), pod(agent, Reading.UNREACHABLE, "", age=-5.0)]
        uncertain = observer.observe("fixture", agent, watch_lost, NOW + 5)
        restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        restored.restore("fixture", store.export("fixture"))
        restarted = Observer(restored, "kubernetes", LIMITS)
        relisted = [beat(agent, -10.0), pod(agent, age=-10.0), signal(agent, Source.SUPERVISOR, value="confirmed")]
        recovered = restarted.observe("fixture", agent, relisted, NOW + 10)
        replayed = restarted.observe("fixture", agent, watch_lost, NOW + 20)
        agents = [a.execution_id for a in restored.agents("fixture")]
        conservative = Observer(restored, "kubernetes", replace(LIMITS, mode=Mode.CONSERVATIVE))
        gone = [pod(agent, Reading.NOT_FOUND, "", age=-30.0)]
        held = [conservative.observe("fixture", agent, gone, NOW + at) for at in (30, 30 + 3600)]
        runs.append(
            {
                "passed": stale.state is State.SUSPECT
                and uncertain.state is State.SUSPECT
                and recovered.state is State.WORKING
                and recovered.recovered
                and replayed == recovered
                and agents == [agent.execution_id]
                and restored.execution("fixture", agent.execution_id).generation == agent.generation
                and all(seen.state is State.SUSPECT and seen.needs_operator for seen in held),
                "before_relist": uncertain.failure,
                "after_relist": {"state": recovered.state, "confidence": recovered.confidence},
                "agents_started": len(agents) - 1,
                "stale_replay_overwrote": replayed != recovered,
                "conservative_rollback": [
                    {"state": seen.state, "needs_operator": seen.needs_operator} for seen in held
                ],
                "execution_observation_age_seconds": restarted.execution_observation_age_seconds("fixture", NOW + 10),
            }
        )
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}
