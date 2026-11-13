import pytest

from scripts.tick.steps.budgets import floor_of
from tests.tick_scale import gate

pytestmark = pytest.mark.unit


def _record(step: str, wall_s: float, **fields: object) -> dict[str, object]:
    return {"step": step, "wall_s": wall_s, "outcome": "success", "rested": False, "abandoned": False, **fields}


def _all_kept() -> list[dict[str, object]]:
    return [_record(name, 0.1) for name in sorted(gate.GATED)]


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_the_gate_covers_the_spawn_chain_reap_and_the_tick_view() -> None:
    assert {"spawn", "reap", "tick view", "drained resume", "recover outages", "verify"} <= gate.GATED


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_every_gated_step_inside_its_floor_passes() -> None:
    assert gate.misses(_all_kept()) == []


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_a_wall_over_the_floor_is_a_miss() -> None:
    records = [*_all_kept(), _record("reap", floor_of("reap") + 0.5)]
    assert gate.misses(records) == [f"reap took {floor_of('reap') + 0.5:.2f}s against its {floor_of('reap'):g}s floor"]


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_a_wall_at_the_floor_passes() -> None:
    assert gate.misses([*_all_kept(), _record("spawn", floor_of("spawn"))]) == []


@pytest.mark.parametrize("flag", ["abandoned", "cut"])
@pytest.mark.feature("tick-scale-gate-step-floors")
def test_a_cut_or_abandoned_step_is_a_miss(flag: str) -> None:
    records = [*_all_kept(), _record("tick view", 1.0, **{flag: True})]
    assert gate.misses(records) == ["tick view was cut off after 1.00s against its 3s floor"]


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_a_step_that_did_not_end_ok_is_a_miss() -> None:
    records = [*_all_kept(), _record("reap", 0.5, outcome="skipped")]
    assert gate.misses(records) == ["reap ended skipped after 0.50s"]


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_a_gated_step_that_only_rested_never_ran() -> None:
    records = [record for record in _all_kept() if record["step"] != "verify"]
    records.append(_record("verify", 99.0, rested=True, outcome="skipped"))
    assert gate.misses(records) == ["verify never ran"]


@pytest.mark.feature("tick-scale-gate-step-floors")
def test_steps_outside_the_gate_are_ignored() -> None:
    assert gate.misses([*_all_kept(), _record("metrics record", 999.0, outcome="failed")]) == []


@pytest.mark.parametrize("url", ["redis://redis.swarm.svc:6379/1", "redis://10.0.0.5:6379/0"])
@pytest.mark.feature("tick-scale-gate-step-floors")
def test_the_gate_refuses_a_redis_that_is_not_loopback_before_flushing_it(url: str) -> None:
    with pytest.raises(SystemExit) as refused:
        gate.main(["--redis", url, "--report", "report.json"])
    assert refused.value.code == 2
