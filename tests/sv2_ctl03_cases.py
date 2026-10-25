import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_v2.admission import (
    ADMITTED,
    DEFERRED,
    IMPOSSIBLE,
    REPLAYED,
    PendingAdmission,
    Policy,
    Resources,
    Template,
)

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/pending-admission.json"


def inputs():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def policy(data, global_cap=None, swarm_cap=None):
    return Policy(
        data["caps"]["global"] if global_cap is None else global_cap,
        data["caps"]["swarm"] if swarm_cap is None else swarm_cap,
        data["pending_ttl_ms"],
        tuple(Template(t["name"], Resources(t["memory_mib"], t["cpu_millis"])) for t in data["templates"]),
        Resources(**data["default_resources"]),
    )


def fresh_store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def build(data, slots=None, **caps):
    store = fresh_store()
    store.create(SwarmConfig(data["swarm"], "agentihooks", 10, 0))
    provider = {"slots": data["provider_slots"] if slots is None else slots, "sessions": 0}
    admission = PendingAdmission(store, policy(data, **caps), lambda slug: provider["slots"])
    return store, admission, provider


def outcomes(decisions):
    return {d.task: d.outcome for d in decisions}


def _keys(store):
    return sorted(store.redis.keys("*"))


def _a(data):
    store, admission, provider = build(data)
    before = _keys(store)
    decisions = admission.admit(data["swarm"], data["tasks"], data["clock_ms"])
    got = outcomes(decisions)
    admitted = [t for t, o in got.items() if o == ADMITTED]
    assert admitted == ["t01", "t02", "t03"]
    assert got[data["impossible"]] == IMPOSSIBLE
    assert [t for t, o in got.items() if o == DEFERRED] == ["t05", "t06", "t07", "t08", "t09", "t10"]
    held = admission.pending(data["swarm"], data["clock_ms"])
    assert sorted(held) == admitted
    assert provider == {"slots": data["provider_slots"], "sessions": 0}
    added = sorted(set(_keys(store)) - set(before))
    assert added == sorted([admission.key(data["swarm"]), admission.total_key(data["swarm"]), admission.global_key])
    return decisions, admission, held


def _b(data):
    store, admission, provider = build(data)
    impossible = next(t for t in data["tasks"] if t["id"] == data["impossible"])
    before = {key: store.redis.dump(key) for key in _keys(store)}
    decisions = admission.admit(data["swarm"], [impossible], data["clock_ms"])
    assert outcomes(decisions) == {data["impossible"]: IMPOSSIBLE}
    assert admission.pending(data["swarm"], data["clock_ms"]) == {}
    assert store.redis.zcard(admission.global_key) == 0
    assert {key: store.redis.dump(key) for key in before} == before
    assert sorted(set(_keys(store)) - set(before)) == [admission.total_key(data["swarm"])]
    assert admission.pending_execution_admission_total(data["swarm"]) == {IMPOSSIBLE: 1}
    again = admission.admit(data["swarm"], [impossible], data["clock_ms"] + data["pending_ttl_ms"] * 4)
    assert outcomes(again) == {data["impossible"]: IMPOSSIBLE}
    assert admission.pending(data["swarm"], data["clock_ms"] + data["pending_ttl_ms"] * 4) == {}
    corrected = {**impossible, "resources": {"memory_mib": 8192}}
    fixed = admission.admit(data["swarm"], [corrected], data["clock_ms"])
    assert outcomes(fixed) == {data["impossible"]: ADMITTED}
    return decisions, admission, {}


def _c(data):
    store, admission, provider = build(data)
    first = admission.admit(data["swarm"], data["tasks"], data["clock_ms"])
    old = {d.task: d for d in first if d.outcome == ADMITTED}
    replay = admission.admit(data["swarm"], data["tasks"], data["clock_ms"] + 1)
    assert {d.task: d.reservation for d in replay if d.outcome == REPLAYED} == {
        t: d.reservation for t, d in old.items()
    }
    assert len(admission.pending(data["swarm"], data["clock_ms"] + 1)) == 3
    restarted = PendingAdmission(store, policy(data), lambda slug: provider["slots"])
    after_restart = restarted.admit(data["swarm"], data["tasks"][:3], data["clock_ms"] + 2)
    assert {d.task: (d.outcome, d.reservation) for d in after_restart} == {
        t: (REPLAYED, d.reservation) for t, d in old.items()
    }
    assert restarted.pending_execution_admission_total(data["swarm"])[ADMITTED] == 3
    provider["slots"] = 0
    outage = data["clock_ms"] + data["pending_ttl_ms"]
    during = admission.admit(data["swarm"], data["tasks"], outage)
    assert ADMITTED not in outcomes(during).values()
    assert admission.pending(data["swarm"], outage) == {}
    assert store.redis.zcard(admission.global_key) == 0
    provider["slots"] = data["provider_slots"]
    later = admission.admit(data["swarm"], data["tasks"], outage + 1)
    renewed = {d.task: d for d in later if d.outcome == ADMITTED}
    assert sorted(renewed) == ["t01", "t02", "t03"]
    assert all(renewed[t].reservation != old[t].reservation for t in renewed)
    assert admission.activate(data["swarm"], "t01", old["t01"].reservation) is False
    assert admission.release(data["swarm"], "t02", old["t02"].reservation) is False
    assert sorted(admission.pending(data["swarm"], outage + 1)) == ["t01", "t02", "t03"]
    disabled = PendingAdmission(store, policy(data, global_cap=0, swarm_cap=0), lambda slug: provider["slots"])
    assert ADMITTED not in outcomes(disabled.admit(data["swarm"], data["tasks"][3:], outage + 2)).values()
    assert disabled.activate(data["swarm"], "t01", renewed["t01"].reservation) is True
    return later, admission, admission.pending(data["swarm"], outage + 2)


def run_case(case):
    data = inputs()
    decisions, admission, held = {"a": _a, "b": _b, "c": _c}[case](data)
    return {
        "case": f"T-SV2-CTL-03-{case.upper()}",
        "state": "passed",
        "evidence_class": "mocked Redis and fake clock; empty fixture cluster; no Kubernetes or provider calls",
        "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "decisions": [{k: v for k, v in asdict(d).items() if k != "reservation"} for d in decisions],
        "pending": sorted(held),
        "pending_execution_admission_total": admission.pending_execution_admission_total(data["swarm"]),
        "rollback": "admission set to zero; held attempt still activates" if case == "c" else "not exercised",
    }
