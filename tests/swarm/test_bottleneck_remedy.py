import json
from dataclasses import replace

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, bottleneck_remedy, bottleneck_status, dispatcher, metrics
from scripts.swarm.health import standing
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import (
    ASSIST,
    DELEGATE,
    FULL,
    MANUAL_SCALING,
    AgentRecord,
    RedisStore,
    SwarmConfig,
    SwarmError,
)

SLUG = "sw"
NOW = 1_800_000_000_000
HOUR = 3_600_000
LATER = NOW + bottleneck.WINDOW_MS
RULE = bottleneck_remedy.RULE
CLAIM = "seat wait at 7.5 task hours in the window, over its 2 threshold"
REVIEW = "review at 5 task hours in the window, over its 2 threshold"
FAILED = "launch failure at 0.5 task hours, over its 0.25 threshold"
HELD = "quota hold at 3 task hours, over its 2 threshold"
REVIEW_FIX = "Bottleneck fix for the review stage"


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=2, scaling=MANUAL_SCALING))
    return saved


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def shipped(monkeypatch):
    rows = []
    monkeypatch.setattr(metrics, "record", lambda table, found, now_ms: rows.append(found) or [])
    return rows


class FakeLedger:
    def __init__(self, refuse=False):
        self.ranks, self.comments, self.refuse = [], [], refuse

    def rank_task(self, slug, task_id, rank, by, if_owned=False):
        if self.refuse:
            raise LedgerRefused("task r1 has a rank the operator set")
        self.ranks.append((slug, task_id, rank, by, if_owned))

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


def fire(store, stage, value=5.0, threshold=2.0, at=NOW, unit="task hours in the window", episode="e1"):
    standing.fire(store.redis, SLUG, stage, standing.Reading(value, threshold, unit, episode), at)


def detect(store, stage, hours, threshold, labels=(), at=NOW):
    reading = {
        "stage": stage,
        "seconds": hours * 3600,
        "previous_seconds": 0.0,
        "trend": "rising",
        "labels": list(labels),
        "label": ", ".join(labels),
        "episodes": [f"d{at}"],
    }
    standing.update_detector(store.redis, SLUG, reading, at, threshold)


def run(store, ledger=None, doc=None, at=NOW):
    found = ledger or FakeLedger()
    return bottleneck_remedy.remedy_pass(SLUG, store, found, doc or {"tasks": []}, at)


def master_items(store):
    return [item.text for item in InboxStore(store.redis).inbox(Mail(InboxStore(store.redis), store, SLUG).master)]


def recorded(store):
    raw = store.redis.hgetall(store.key(SLUG, bottleneck_status.ACTIONS))
    return {stage: json.loads(value) for stage, value in raw.items()}


def stamped(store):
    return store.redis.hgetall(store.key(SLUG, bottleneck_remedy.KEY))


def logged(home):
    return [
        (row["at"], row["gate"], row["kind"], row["task"], row["tool"], row["reason"])
        for row in gate_log.recent(SLUG, None, home)
    ]


def caps(store):
    config = store.config(SLUG)
    return config.max_eng, config.max_ci


def pin(store, lane, agent, model="opus"):
    lanes = {key: dict(value) for key, value in store.config(SLUG).lanes.items()}
    lanes.setdefault(lane, {}).update(agent=agent, model=model)
    store.update(SLUG, lanes=lanes)


def lane_of(store, lane):
    found = store.config(SLUG).lanes.get(lane, {})
    return found.get("agent", "auto"), found.get("model", "auto")


def fix_task(rank=None, state="open"):
    task = {"id": "r1", "title": REVIEW_FIX, "state": state, "lane": "eng"}
    return {**task, "rank": rank} if rank else task


def claim_fix(rank=None):
    return {**fix_task(rank), "title": "Bottleneck fix for the seat wait stage"}


def proposal(stage, targeted, why, fix=None, fired=1, task=None):
    times = "time" if fired == 1 else "times"
    fix = fix or f"Bottleneck fix for the {stage} stage"
    ask = bottleneck_remedy.RAISE.format(task=task, fix=fix) if task else bottleneck_remedy.ADD.format(fix=fix)
    return bottleneck_remedy.PROPOSE.format(label=stage, targeted=targeted, fired=fired, times=times, why=why, ask=ask)


def proposed(targeted, why):
    return bottleneck_remedy.PROPOSED.format(targeted=targeted, why=why)


@pytest.mark.parametrize("autonomy", [DELEGATE, FULL])
def test_seat_wait_moves_no_seat_and_proposes_to_the_master(store, home, shipped, autonomy):
    store.update(SLUG, autonomy=autonomy)
    fire(store, "seat_wait", value=7.5)
    text = proposed(CLAIM, bottleneck_remedy.UNMECHANICAL)
    assert run(store) == [text]
    assert caps(store) == (2, 2)
    assert recorded(store) == {"seat_wait": {"text": text, "at": NOW}}
    assert stamped(store) == {"seat_wait": str(NOW)}
    assert logged(home) == [(NOW, "dispatcher", "propose", "", RULE, text)]
    assert master_items(store) == [proposal("seat wait", CLAIM, bottleneck_remedy.UNMECHANICAL)]


@pytest.mark.parametrize("shift", [3, 16, -16])
def test_seat_wait_under_auto_scaling_leaves_the_stored_lane_shift(store, home, shipped, shift):
    store.update(SLUG, scaling="auto", lane_shift=shift)
    fire(store, "seat_wait", value=7.5)
    assert run(store) == [proposed(CLAIM, bottleneck_remedy.UNMECHANICAL)]
    assert store.config(SLUG).lane_shift == shift
    assert caps(store) == (2, 2)


def test_one_action_per_stage_per_window(store, home, shipped):
    fire(store, "seat_wait", value=7.5)
    run(store)
    assert run(store, at=LATER - 1) == []
    fire(store, "seat_wait", value=7.5, at=LATER)
    assert run(store, at=LATER) == [proposed(CLAIM, bottleneck_remedy.UNMECHANICAL)]
    assert caps(store) == (2, 2)
    assert stamped(store) == {"seat_wait": str(LATER)}


def test_a_change_the_swarm_refuses_is_proposed(store, home, shipped, monkeypatch):
    pin(store, "ci", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))

    def refuse(*args, **kwargs):
        raise SwarmError("boom")

    monkeypatch.setattr(store, "update", refuse)
    why = bottleneck_remedy.REFUSED.format(error="boom")
    assert run(store) == [proposed(FAILED, why)]
    assert lane_of(store, "ci") == ("claude", "opus")


def test_launch_failure_moves_the_lane_pinned_to_the_failing_harness_once(store, home, shipped):
    pin(store, "eng", "codex")
    pin(store, "ci", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    text = bottleneck_remedy.MOVED.format(lane="ci", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text]
    assert lane_of(store, "ci") == ("codex", "auto")
    assert lane_of(store, "eng") == ("codex", "opus")
    assert recorded(store)["launch_failure"]["text"] == text
    assert logged(home) == [(NOW, "dispatcher", "apply", "", RULE, text)]
    assert master_items(store) == []
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",), at=LATER)
    why = bottleneck_remedy.UNPINNED.format(harness="claude")
    assert run(store, at=LATER) == [proposed(FAILED, why)]
    assert lane_of(store, "ci") == ("codex", "auto")


@pytest.mark.parametrize("labels", [(), ("claude", "codex")])
def test_launch_failure_without_one_failing_harness_is_proposed(store, home, shipped, labels):
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=labels)
    assert run(store) == [proposed(FAILED, bottleneck_remedy.UNKNOWN)]
    assert lane_of(store, "eng") == ("claude", "opus")


def hive_only(store, monkeypatch, excluded="eng", herdr=True, deployment="local"):
    from scripts import herdr_host
    from scripts.hive import registry
    from scripts.swarm import commands

    store.update(SLUG, hive_placement=True)
    if excluded:
        registry.update(store.redis, "ws", [f"excluded={excluded}"])
    monkeypatch.setattr(commands, "hive_id", lambda: "ws")
    monkeypatch.setattr(
        herdr_host, "server_running", lambda environ: herdr and environ["AGENTIHOOKS_DEPLOYMENT"] == deployment
    )
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)


@pytest.mark.parametrize("excluded", ["eng", "engineer", "plan,engineer"])
def test_launch_failure_keeps_a_hive_only_lane_on_the_harness_hives_place(store, home, shipped, monkeypatch, excluded):
    hive_only(store, monkeypatch, excluded=excluded)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    assert why == "no eligible host places codex for the eng lane"
    assert run(store) == [proposed(FAILED, why)]
    assert lane_of(store, "eng") == ("claude", "opus")
    assert logged(home) == [(NOW, "dispatcher", "propose", "", RULE, proposed(FAILED, why))]
    assert master_items(store) == [proposal("launch failure", FAILED, why)]


def test_launch_failure_moves_a_lane_the_workstation_hosts_off_claude(store, home, shipped, monkeypatch):
    hive_only(store, monkeypatch, excluded="plan")
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text]
    assert lane_of(store, "eng") == ("codex", "auto")


def test_launch_failure_moves_a_lane_off_claude_with_no_workstation_record(store, home, shipped, monkeypatch):
    hive_only(store, monkeypatch, excluded="")
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text]


@pytest.mark.parametrize(("herdr", "deployment"), [(False, "local"), (True, "compose"), (True, "distributed")])
def test_launch_failure_keeps_a_lane_on_claude_when_the_workstation_takes_no_overflow(
    store, home, shipped, monkeypatch, herdr, deployment
):
    hive_only(store, monkeypatch, excluded="plan", herdr=herdr, deployment=deployment)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    assert run(store) == [proposed(FAILED, why)]
    assert lane_of(store, "eng") == ("claude", "opus")


def test_launch_failure_moves_a_lane_the_workstation_excludes_without_hive_placement(store, home, shipped, monkeypatch):
    hive_only(store, monkeypatch)
    store.update(SLUG, hive_placement=False)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text]


@pytest.mark.parametrize(("limit", "occupied", "moves"), [(1, 0, False), (2, 0, True), (3, 1, True), (3, 2, False)])
def test_workstation_only_move_requires_room_for_the_lane(store, home, shipped, monkeypatch, limit, occupied, moves):
    from scripts.swarm import local_slots

    hive_only(store, monkeypatch, excluded="plan")
    store.update(SLUG, local_max_agents=limit)
    for index in range(occupied):
        store.redis.hset(local_slots.key("ws"), f"other/worker{index}", str(NOW))
    monkeypatch.setattr(local_slots.lease, "now_ms", lambda store: NOW)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text if moves else proposed(FAILED, why)]
    assert lane_of(store, "eng") == (("codex", "auto") if moves else ("claude", "opus"))


@pytest.mark.parametrize(
    ("agent", "limit", "moves"),
    [
        (AgentRecord("worker", "eng", "task"), 2, True),
        (AgentRecord("worker", "eng", "task"), 1, False),
        (AgentRecord("worker", "ci", "task"), 2, False),
        (AgentRecord("worker", "eng", "task", state="finished"), 1, False),
        (AgentRecord("worker", "eng", "task", hive="remote", seat=f"eng-1@{SLUG}"), 1, False),
    ],
)
def test_workstation_only_move_counts_existing_local_lane_workers_once(
    store, home, shipped, monkeypatch, agent, limit, moves
):
    from scripts.swarm import local_slots

    hive_only(store, monkeypatch, excluded="plan")
    store.update(SLUG, local_max_agents=limit)
    store.start_execution(SLUG, replace(agent, name=store.next_name(SLUG, agent.lane), seat=f"{agent.lane}-1@{SLUG}"))
    monkeypatch.setattr(local_slots.lease, "now_ms", lambda store: NOW)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text if moves else proposed(FAILED, why)]
    assert lane_of(store, "eng") == (("codex", "auto") if moves else ("claude", "opus"))


@pytest.mark.parametrize("task", ["None", "XXXX"])
def test_workstation_only_move_counts_other_lane_workers_for_any_task_name(store, home, shipped, monkeypatch, task):
    from scripts.swarm import local_slots

    hive_only(store, monkeypatch, excluded="plan")
    store.update(SLUG, local_max_agents=2)
    store.put_agent(SLUG, AgentRecord("worker", "ci", task))
    monkeypatch.setattr(local_slots.lease, "now_ms", lambda store: NOW)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    assert run(store) == [proposed(FAILED, why)]
    assert lane_of(store, "eng") == ("claude", "opus")


@pytest.mark.parametrize(("count", "moves"), [(2, True), (3, False)])
def test_workstation_only_move_does_not_credit_workers_above_the_lane_limit(
    store, home, shipped, monkeypatch, count, moves
):
    from scripts.swarm import local_slots

    hive_only(store, monkeypatch, excluded="plan")
    store.update(SLUG, local_max_agents=2)
    for index in range(count):
        store.put_agent(SLUG, AgentRecord(f"worker{index}", "eng", f"task{index}"))
    monkeypatch.setattr(local_slots.lease, "now_ms", lambda store: NOW)
    pin(store, "eng", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    why = bottleneck_remedy.UNPLACEABLE.format(target="codex", lane="eng")
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=FAILED)
    assert run(store) == [text if moves else proposed(FAILED, why)]
    assert lane_of(store, "eng") == (("codex", "auto") if moves else ("claude", "opus"))


@pytest.mark.parametrize("source_room", [0, 1, 8])
def test_quota_hold_keeps_a_lane_on_a_harness_with_available_accounts(store, home, shipped, source_room):
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"placeable": {"claude": source_room, "codex": 9}}))
    pin(store, "eng", "claude")
    detect(store, "quota_hold", 3.0, 2.0, labels=("claude",))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="claude", target="codex", targeted=HELD)
    why = "claude accounts still have room"
    assert run(store) == [proposed(HELD, why) if source_room else text]
    assert lane_of(store, "eng") == (("claude", "opus") if source_room else ("codex", "auto"))


@pytest.mark.parametrize(
    ("hive", "moves"),
    [
        ({"room": 2}, True),
        ({"room": 1}, True),
        ({"room": 0}, False),
        ({"room": 2, "heartbeat_at": NOW - 10 * 60_000}, False),
        ({"room": 2, "excluded": ["eng"]}, False),
        ({"room": 2, "excluded": ["engineer"]}, False),
        ({"room": 2, "draining": "yes"}, False),
    ],
)
def test_quota_hold_moves_a_hive_only_lane_back_to_claude_only_into_hive_room(
    store, home, shipped, monkeypatch, hive, moves
):
    from scripts.hive import launch
    from scripts.swarm import lease

    hive_only(store, monkeypatch)
    loaded = {"id": "h1", "excluded": [], "draining": "no", "heartbeat_at": NOW, **hive}
    monkeypatch.setattr(launch, "loaded_hives", lambda found: [loaded] if found is store else [])
    monkeypatch.setattr(lease, "now_ms", lambda found: NOW if found is store else 0)
    pin(store, "eng", "codex")
    detect(store, "quota_hold", 3.0, 2.0, labels=("codex",))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="codex", target="claude", targeted=HELD)
    why = bottleneck_remedy.UNPLACEABLE.format(target="claude", lane="eng")
    assert run(store) == [text if moves else proposed(HELD, why)]
    assert lane_of(store, "eng") == (("claude", "auto") if moves else ("codex", "opus"))


def test_quota_hold_moves_the_lane_pinned_to_the_held_harness_once(store, home, shipped):
    pin(store, "ci", "codex")
    detect(store, "quota_hold", 3.0, 2.0, labels=("codex",))
    text = bottleneck_remedy.MOVED.format(lane="ci", source="codex", target="claude", targeted=HELD)
    assert run(store) == [text]
    assert lane_of(store, "ci") == ("claude", "auto")
    detect(store, "quota_hold", 3.0, 2.0, labels=("codex",), at=LATER)
    why = bottleneck_remedy.UNPINNED.format(harness="codex")
    assert run(store, at=LATER) == [proposed(HELD, why)]
    assert master_items(store) == [proposal("quota hold", HELD, why, fired=2)]


def test_quota_hold_naming_no_harness_is_proposed(store, home, shipped):
    pin(store, "ci", "codex")
    detect(store, "quota_hold", 3.0, 2.0)
    assert run(store) == [proposed(HELD, bottleneck_remedy.UNKNOWN)]
    assert lane_of(store, "ci") == ("codex", "opus")


def test_quota_hold_naming_both_harnesses_over_its_window_moves_the_one_lane_pinned_to_either(store, home, shipped):
    pin(store, "eng", "codex")
    detect(store, "quota_hold", 3.0, 2.0, labels=("claude", "codex"))
    text = bottleneck_remedy.MOVED.format(lane="eng", source="codex", target="claude", targeted=HELD)
    assert run(store) == [text]
    assert lane_of(store, "eng") == ("claude", "auto")


def test_quota_hold_naming_both_harnesses_with_both_pinned_is_proposed(store, home, shipped):
    pin(store, "eng", "codex")
    pin(store, "ci", "claude")
    detect(store, "quota_hold", 3.0, 2.0, labels=("claude", "codex"))
    assert run(store) == [proposed(HELD, bottleneck_remedy.UNKNOWN)]
    assert (lane_of(store, "eng"), lane_of(store, "ci")) == (("codex", "opus"), ("claude", "opus"))


def test_quota_held_samples_on_one_harness_move_its_pinned_lane_through_the_detector(store, home, shipped):
    from scripts.swarm import stage_detector
    from tests.swarm import test_stages as recorded_stages

    now = recorded_stages.at(9, 30)
    held = [
        {**recorded_stages.sample(at, 1, "quota"), "held_harness": "codex"}
        for at in range(recorded_stages.at(7), now, bottleneck.SAMPLE_GAP_MS)
    ]
    found = stage_detector.report(
        recorded_stages.rows(
            ledger_events=[recorded_stages.ledger("task done", recorded_stages.at(7), "tasks/t0")],
            host_samples=held,
            agent_events=[recorded_stages.agent("claim", "t1", now)],
        ),
        [{**recorded_stages.TASK, "state": "claimed"}],
        now,
    )
    stage_detector.update_findings(store, SLUG, found, now)
    pin(store, "eng", "codex")
    targeted = "quota hold at 2.5 task hours, over its 2 threshold"
    text = bottleneck_remedy.MOVED.format(lane="eng", source="codex", target="claude", targeted=targeted)
    assert run(store, at=now) == [text]
    assert lane_of(store, "eng") == ("claude", "auto")


def test_a_stage_with_a_fix_task_raises_it_to_urgent(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    text = bottleneck_remedy.RAISED.format(task="r1", targeted=REVIEW)
    assert run(store, ledger=ledger, doc={"tasks": [{"id": "x"}, fix_task()]}) == [text]
    assert ledger.ranks == [(SLUG, "r1", "urgent", "dispatcher", True)]
    assert ledger.comments == [(SLUG, "r1", text, "dispatcher")]
    assert recorded(store)["review"]["text"] == text
    assert logged(home) == [(NOW, "dispatcher", "apply", "r1", RULE, text)]
    assert master_items(store) == []


def test_an_urgent_fix_task_needs_no_further_action(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    assert run(store, ledger=ledger, doc={"tasks": [fix_task(rank="urgent")]}) == []
    assert ledger.ranks == [] and master_items(store) == [] and recorded(store) == {} and stamped(store) == {}


def test_a_done_fix_task_is_no_fix_and_the_stage_is_proposed(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    run(store, ledger=ledger, doc={"tasks": [fix_task(state="done")]})
    assert ledger.ranks == []
    assert master_items(store) == [proposal("review", REVIEW, bottleneck_remedy.UNMECHANICAL)]


def test_a_refused_rank_proposes_raising_the_existing_task(store, home, shipped):
    fire(store, "review")
    why = "the ledger kept the rank of task r1: task r1 has a rank the operator set"
    assert run(store, ledger=FakeLedger(refuse=True), doc={"tasks": [fix_task()]}) == [proposed(REVIEW, why)]
    assert master_items(store) == [proposal("review", REVIEW, why, fix=REVIEW_FIX, task="r1")]


def test_a_mechanical_remedy_runs_before_its_fix_task_is_raised(store, home, shipped):
    pin(store, "ci", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    ledger = FakeLedger()
    fix = {**fix_task(), "title": "Bottleneck fix for the launch failure stage"}
    text = bottleneck_remedy.MOVED.format(lane="ci", source="claude", target="codex", targeted=FAILED)
    assert run(store, ledger=ledger, doc={"tasks": [fix]}) == [text]
    assert ledger.ranks == []


def test_seat_wait_raises_its_fix_task(store, home, shipped):
    fire(store, "seat_wait", value=7.5)
    ledger = FakeLedger()
    text = bottleneck_remedy.RAISED.format(task="r1", targeted=CLAIM)
    assert run(store, ledger=ledger, doc={"tasks": [claim_fix()]}) == [text]
    assert ledger.ranks == [(SLUG, "r1", "urgent", "dispatcher", True)]
    assert caps(store) == (2, 2)


def test_seat_wait_with_an_urgent_fix_task_does_nothing(store, home, shipped):
    fire(store, "seat_wait", value=7.5)
    assert run(store, doc={"tasks": [claim_fix(rank="urgent")]}) == []
    assert master_items(store) == [] and stamped(store) == {}


def test_a_non_mechanical_stage_sends_exactly_one_proposal_per_window(store, home, shipped):
    fire(store, "merge_queue", value=4.0, threshold=1.5)
    targeted = "merge queue at 4 task hours in the window, over its 1.5 threshold"
    text = proposed(targeted, bottleneck_remedy.UNMECHANICAL)
    assert run(store) == [text]
    assert run(store, at=NOW + HOUR) == []
    assert master_items(store) == [proposal("merge queue", targeted, bottleneck_remedy.UNMECHANICAL)]
    assert recorded(store) == {"merge_queue": {"text": text, "at": NOW}}
    assert logged(home) == [(NOW, "dispatcher", "propose", "", RULE, text)]
    fire(store, "merge_queue", value=4.0, threshold=1.5, at=LATER, episode="e2")
    assert run(store, at=LATER) == [text]
    assert master_items(store)[1] == proposal("merge queue", targeted, bottleneck_remedy.UNMECHANICAL, fired=2)


@pytest.mark.parametrize(("doc", "ask"), [({"tasks": []}, None), ({"tasks": [claim_fix()]}, "r1")])
def test_below_delegate_a_mechanical_stage_is_proposed(store, home, shipped, doc, ask):
    store.update(SLUG, autonomy=ASSIST)
    fire(store, "seat_wait", value=7.5)
    why = bottleneck_remedy.BELOW.format(autonomy=ASSIST)
    assert run(store, doc=doc) == [proposed(CLAIM, why)]
    assert caps(store) == (2, 2)
    fix = "Bottleneck fix for the seat wait stage"
    assert master_items(store) == [proposal("seat wait", CLAIM, why, fix=fix, task=ask)]


def test_below_delegate_an_urgent_fix_task_needs_no_proposal(store, home, shipped):
    store.update(SLUG, autonomy=ASSIST)
    fire(store, "seat_wait", value=7.5)
    assert run(store, doc={"tasks": [claim_fix(rank="urgent")]}) == []
    assert master_items(store) == []


def test_a_stale_finding_runs_no_remedy(store, home, shipped):
    fire(store, "seat_wait", at=NOW - standing.STALE_MS)
    assert run(store) == []
    assert caps(store) == (2, 2)


@pytest.mark.parametrize(("hours", "threshold"), [(2.0, 2.0), (1.0, 0.0)])
def test_a_reading_not_over_a_threshold_runs_no_remedy(store, home, shipped, hours, threshold):
    detect(store, "seat_wait", hours, threshold)
    assert run(store) == []
    assert caps(store) == (2, 2) and master_items(store) == [] and recorded(store) == {}


def test_a_stage_not_due_leaves_the_next_stage_its_remedy(store, home, shipped):
    fire(store, "seat_wait", at=NOW - standing.STALE_MS)
    fire(store, "merge_queue", value=4.0, threshold=1.5)
    assert len(run(store)) == 1
    assert list(recorded(store)) == ["merge_queue"]


def test_a_stage_needing_no_action_leaves_the_next_stage_its_remedy(store, home, shipped):
    fire(store, "seat_wait", value=7.5)
    fire(store, "merge_queue", value=4.0, threshold=1.5)
    assert len(run(store, doc={"tasks": [claim_fix(rank="urgent")]})) == 1
    assert list(stamped(store)) == ["merge_queue"]


def test_actions_taken_before_a_failing_stage_are_still_logged(store, home, shipped, monkeypatch):
    pin(store, "ci", "claude")
    detect(store, "launch_failure", 0.5, 0.25, labels=("claude",))
    fire(store, "merge_queue", value=4.0, threshold=1.5)

    def broken(*args, **kwargs):
        raise RuntimeError("inbox down")

    monkeypatch.setattr(InboxStore, "send", broken)
    with pytest.raises(RuntimeError):
        run(store)
    text = bottleneck_remedy.MOVED.format(lane="ci", source="claude", target="codex", targeted=FAILED)
    assert logged(home) == [(NOW, "dispatcher", "apply", "", RULE, text)]


def test_a_ci_overrun_proposal_names_its_own_unit(store, home, shipped):
    fire(store, "ci_overrun", value=22.5, threshold=15.0, unit="minutes from push to Gate Required")
    targeted = "ci overrun at 22.5 minutes from push to Gate Required, over its 15 threshold"
    assert run(store) == [proposed(targeted, bottleneck_remedy.UNMECHANICAL)]


def test_the_step_runs_the_remedies_on_its_ledger_state_and_clock(store, home, shipped):
    fire(store, "seat_wait", value=7.5)
    fire(store, "review")
    ledger, doc = FakeLedger(), {"tasks": [fix_task()]}
    found = bottleneck_remedy.step(SLUG, store, ledger, doc, NOW)
    assert sorted(found[:2]) == sorted(
        [
            bottleneck_remedy.RAISED.format(task="r1", targeted=REVIEW),
            proposed(CLAIM, bottleneck_remedy.UNMECHANICAL),
        ]
    )
    assert ledger.ranks == [(SLUG, "r1", "urgent", "dispatcher", True)]
    assert stamped(store) == {"seat_wait": str(NOW), "review": str(NOW)}


def test_the_tick_hands_its_store_ledger_state_and_clock_to_the_remedies(monkeypatch):
    import fakeredis

    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger as TickLedger
    from tests.swarm.test_tick import FakeRuntime

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    seen = []
    monkeypatch.setattr(bottleneck_remedy, "step", lambda *a: seen.append(a) or [])
    ledger = TickLedger([{"id": "a"}])
    tick.tick(SLUG, saved, ledger, FakeRuntime(), now_ms=4_321)
    assert [(a[0], a[1], a[2], a[3]["tasks"][0]["id"], a[4]) for a in seen] == [(SLUG, saved, ledger, "a", 4_321)]


def test_the_dispatcher_queue_puts_an_urgent_fix_task_first_and_ignores_its_lower_ranks():
    common = {"state": "open", "lane": "eng", "phase": "p1", "depends_on": []}
    tasks = [
        {"id": "a1", "title": "a", **common},
        {"id": "h1", "title": "h", "rank": "high", **common},
        {"id": "r1", "title": REVIEW_FIX, "rank": "urgent", **common},
    ]
    stamps = {f"tasks/{task_id}/rank": {"by": "dispatcher"} for task_id in ("h1", "r1")}
    doc = {"overview": "o", "tasks": tasks, "phases": [{"id": "p1", "depends_on": []}], "_meta": {"stamps": stamps}}
    assert dispatcher.queue(doc, "") == ["r1", "a1", "h1"]
