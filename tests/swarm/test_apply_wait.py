import pytest

from scripts.swarm import apply_wait
from scripts.swarm.store import SwarmConfig

DOCTOR = SwarmConfig("sw-doctor", "/repo", 1, 0, template="doctor")
PLAIN = SwarmConfig("sw", "/repo", 1, 0)
AT = "2026-10-18T12:00:00Z"


def rows(**deps):
    found = {"m": {"id": "m", "kind": "tune", "state": "open", "depends_on": list(deps)}}
    for name, fields in deps.items():
        found[name] = {"id": name, "state": "done", **fields}
    return found


@pytest.mark.parametrize(
    ("deps", "waiting"),
    [
        ({"a": {}}, ["a"]),
        ({"a": {"kind": "ci"}}, ["a"]),
        ({"a": {"applied_at": AT}}, []),
        ({"a": {"kind": "tune"}}, []),
        ({"a": {"kind": "ops"}}, []),
        ({"a": {"kind": "plan"}}, []),
        ({"a": {"applied_at": AT}, "b": {"kind": "code"}}, ["b"]),
    ],
)
def test_a_tune_task_waits_on_each_code_fix_the_doctor_has_not_applied(deps, waiting):
    found = rows(**deps)
    assert apply_wait.unapplied(found["m"], found) == waiting
    assert apply_wait.checker(DOCTOR, found)(found["m"]) is (not waiting)


def test_a_task_of_another_kind_never_waits_on_an_applied_time():
    found = rows(a={})
    found["m"]["kind"] = "code"
    assert apply_wait.unapplied(found["m"], found) == []


def test_a_swarm_that_is_not_a_doctor_never_waits_on_an_applied_time():
    found = rows(a={})
    assert apply_wait.checker(PLAIN, found)(found["m"]) is True


def test_a_tune_task_waits_on_a_dependency_missing_from_the_rows():
    found = rows(a={})
    del found["a"]
    assert apply_wait.unapplied(found["m"], found) == ["a"]


@pytest.mark.parametrize(("template", "held"), [("doctor", True), ("", False)])
def test_the_tick_claims_a_doctor_tune_task_only_after_its_fix_is_applied(tmp_path, template, held):
    import fakeredis

    from scripts.swarm.store import RedisStore
    from scripts.swarm.tick import _claimable

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", str(tmp_path), max_eng=0, max_ci=1, template=template))
    found = rows(a={"lane": "eng"})
    found["m"]["lane"] = "ci"

    def claimable():
        return [t["id"] for t in _claimable("sw", store, found, {"phases": [], "tasks": list(found.values())}, "ci")]

    assert claimable() == ([] if held else ["m"])
    found["a"]["applied_at"] = AT
    assert claimable() == ["m"]
