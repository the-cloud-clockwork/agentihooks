import fakeredis
import pytest

from scripts.doctor import detect, master_prompts
from scripts.swarm import input_wait
from scripts.swarm.health.verdicts import VerdictStore
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import _watch_idle
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit

MINUTE = 60_000
ENTERED = 1_792_000_000_000
PROMPT = "Which plan should the swarm take?"
MASTER = {"name": "master@d44a5a-0002", "lane": "master", "seat": "master@sw", "input_prompt": PROMPT}


def found_at(minutes, agent=MASTER, since=ENTERED):
    return master_prompts.findings([agent], {agent["name"]: since}, ENTERED + minutes * MINUTE)


def test_a_master_prompt_under_fifteen_minutes_is_no_finding():
    assert found_at(14) == []
    assert master_prompts.findings([MASTER], {MASTER["name"]: ENTERED}, ENTERED + 15 * MINUTE - 1) == []


def test_a_master_prompt_at_fifteen_minutes_names_the_seat_the_prompt_and_the_minutes():
    (finding,) = found_at(15)
    assert finding.id == "master-waiting-on-input/master@sw"
    assert finding.summary == "the master pane has waited on an input prompt for 15 minutes"
    assert finding.evidence == (
        "seat master@sw",
        f"prompt: {PROMPT}",
        "waited 15 minutes since 2026-10-14 17:46:40 UTC",
        "agent master@d44a5a-0002",
    )
    assert finding.threshold == "a master pane on one input prompt for 15 minutes, again each further 15 minutes"
    assert finding.measure == 15


def test_the_wait_moves_only_in_fifteen_minute_steps():
    assert [found_at(m)[0].measure for m in (15, 29, 30, 44, 45, 131)] == [15, 15, 30, 30, 45, 120]
    assert found_at(29)[0].evidence == found_at(15)[0].evidence
    assert found_at(30)[0].evidence[2] == "waited 30 minutes since 2026-10-14 17:46:40 UTC"


def test_a_master_without_a_seat_is_named_by_its_agent():
    (finding,) = found_at(15, {**MASTER, "seat": ""})
    assert finding.subject == "master@d44a5a-0002"
    assert finding.evidence[0] == "seat master@d44a5a-0002"


@pytest.mark.parametrize(
    "agent,since",
    [
        ({**MASTER, "lane": "eng"}, ENTERED),
        ({**MASTER, "input_prompt": ""}, ENTERED),
        (MASTER, None),
        (MASTER, 0),
    ],
    ids=["engineer", "no prompt", "no entry time", "zero entry time"],
)
def test_only_a_master_on_a_prompt_with_an_entry_time_is_read(agent, since):
    assert found_at(60, agent, since) == []


def test_a_judged_wait_returns_only_when_it_grows_by_another_fifteen_minutes():
    verdicts = VerdictStore(fakeredis.FakeRedis(decode_responses=True), "doctor-findings")

    def shown(minutes):
        now = ENTERED + minutes * MINUTE
        return [f["id"] for f in verdicts.visible(found_at(minutes), now, 0, new_evidence=True)]

    assert shown(15) == ["master-waiting-on-input/master@sw"]
    verdicts.judge("master-waiting-on-input/master@sw", "established", "operator asked", "master", ENTERED)
    assert [shown(m) for m in (16, 22, 29)] == [[], [], []]
    assert shown(30) == ["master-waiting-on-input/master@sw"]


def watched():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    master = AgentRecord("master@d44a5a-0002", "master", "", seat="master@sw")
    store.put_agent("sw", master)
    runtime = FakeRuntime()
    return store, runtime


def watch(store, runtime, observation, now_ms):
    runtime.observe = lambda agent: observation
    _watch_idle("sw", store, FakeLedger([]), runtime, {}, store.agents("sw")[0], now_ms)


def test_the_tick_keeps_the_entry_time_while_the_same_prompt_holds_and_clears_it_after():
    store, runtime = watched()
    name = "master@d44a5a-0002"
    watch(store, runtime, PaneObservation("waiting", PROMPT), ENTERED)
    watch(store, runtime, PaneObservation("waiting", PROMPT), ENTERED + 5 * MINUTE)
    assert input_wait.since(store, "sw", name) == ENTERED
    watch(store, runtime, PaneObservation("waiting", "Another question?"), ENTERED + 6 * MINUTE)
    assert input_wait.since(store, "sw", name) == ENTERED + 6 * MINUTE
    watch(store, runtime, PaneObservation("idle"), ENTERED + 7 * MINUTE)
    assert input_wait.since(store, "sw", name) is None


def test_a_wait_that_predates_the_entry_time_starts_counting_at_the_next_tick():
    store, runtime = watched()
    store.put_agent("sw", AgentRecord("master@d44a5a-0002", "master", "", seat="master@sw", input_prompt=PROMPT))
    watch(store, runtime, PaneObservation("waiting", PROMPT), ENTERED)
    assert input_wait.since(store, "sw", "master@d44a5a-0002") == ENTERED


def test_the_doctor_reads_the_watched_master_wait_and_never_types_into_its_pane():
    store, runtime = watched()
    watch(store, runtime, PaneObservation("waiting", PROMPT), ENTERED)
    for minute in range(1, 16):
        watch(store, runtime, PaneObservation("waiting", PROMPT), ENTERED + minute * MINUTE)
    early = detect.readers(store, None, "sw", ENTERED + 14 * MINUTE, environ={}, telemetry=False)["master prompt"]()
    late = detect.readers(store, None, "sw", ENTERED + 15 * MINUTE, environ={}, telemetry=False)["master prompt"]()
    assert early == []
    assert [(f.id, f.measure, f.evidence[1]) for f in late] == [
        ("master-waiting-on-input/master@sw", 15, f"prompt: {PROMPT}")
    ]
    assert runtime.typed == {} and runtime.nudged == [] and runtime.killed == []
