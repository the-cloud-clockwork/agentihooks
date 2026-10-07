from dataclasses import asdict

from hooks.observability import correlation
from scripts.doctor import registry, traces
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import Placed, placed_record

TICK = 1_791_381_328_054
LAUNCH = 1_791_381_399_634
NAME = "engineer@323133-0427"


def _starting():
    return AgentRecord(NAME, "eng", "tm2", started_at=TICK, state="starting", seat="eng-2@sw")


def _placed(record):
    return placed_record(record, Placed("%1", "claude", profile="engineer", launched_at=LAUNCH))


def _early_life(record):
    inputs = correlation.Inputs("cefe", {"AGENTIHOOKS_AGENT_NAME": NAME}, "claude", agent=asdict(record))
    return correlation.envelope(inputs)["agent.life"][1]


def test_placement_keeps_the_life_startup_telemetry_read():
    record = _starting()
    early = _early_life(record)
    working = _placed(record)
    assert working.started_at == TICK
    assert early == _early_life(working) == f"{NAME}#{TICK}"


def test_placement_records_the_launch_on_its_own_clock():
    working = _placed(_starting())
    assert working.launched_at == LAUNCH
    assert working.started_at == TICK


def test_the_detector_reports_nothing_for_a_life_that_never_changed():
    record = _starting()
    early = _early_life(record)
    (binding,) = registry.bindings([asdict(_placed(record))])
    trace = {"id": "3948", "life": early, "seat": "eng-2@sw", "task": "tm2", "harness": "claude"}
    assert traces._misattributed({**binding, "traces": [trace], "local": {}, "read": True, "remote": [trace]}) is None
