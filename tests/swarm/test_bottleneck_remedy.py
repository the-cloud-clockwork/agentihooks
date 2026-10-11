import json

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, bottleneck_remedy, bottleneck_status, dispatcher, lane_split, metrics
from scripts.swarm.health import standing
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import ASSIST, MANUAL_SCALING, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
NOW = 1_800_000_000_000
HOUR = 3_600_000
ROOMY = lane_split.Lanes(ready={"eng": 3, "ci": 1}, live={"eng": 1, "ci": 0}, room=4)


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
        self.comments.append((task_id, text, by))


def fire(store, stage, value=5.0, threshold=2.0, at=NOW):
    standing.fire(store.redis, SLUG, stage, standing.Reading(value, threshold, "task hours in the window", "e1"), at)


def run(store, ledger=None, doc=None, lanes=ROOMY, at=NOW):
    config = store.config(SLUG)
    found = ledger or FakeLedger()
    return bottleneck_remedy.remedy_pass(SLUG, config, store, found, doc or {"tasks": []}, lambda: lanes, at)


def master_items(store):
    return InboxStore(store.redis).inbox(Mail(InboxStore(store.redis), store, SLUG).master)


def recorded(store):
    raw = store.redis.hgetall(store.key(SLUG, bottleneck_status.ACTIONS))
    return {stage: json.loads(value) for stage, value in raw.items()}


def caps(store):
    config = store.config(SLUG)
    return config.max_eng, config.max_ci


def pin(store, lane, agent, model="opus"):
    lanes = {key: dict(value) for key, value in store.config(SLUG).lanes.items()}
    lanes.setdefault(lane, {}).update(agent=agent, model=model)
    store.update(SLUG, lanes=lanes)


def fix_task(rank=None, state="open"):
    task = {"id": "r1", "title": "Bottleneck fix for the review stage", "state": state, "lane": "eng"}
    return {**task, "rank": rank} if rank else task


def test_claim_wait_moves_one_seat_from_ci_to_engineers_and_records_the_hours(store, home, shipped):
    fire(store, "claim_wait", value=7.5)
    (text,) = run(store)
    assert caps(store) == (3, 1)
    assert "claim wait at 7.5 task hours in the window, over its 2 threshold" in text
    assert text.startswith("Moved one seat from the ci lane to the eng lane")
    assert recorded(store) == {"claim_wait": {"text": text, "at": NOW}}
    (row,) = gate_log.recent(SLUG, None, home)
    assert (row["gate"], row["kind"], row["tool"], row["reason"]) == ("dispatcher", "apply", "bottleneck-remedy", text)
    assert master_items(store) == []


def test_a_seat_shift_never_passes_a_cap_and_proposes_instead(store, home, shipped):
    fire(store, "claim_wait")
    full = lane_split.Lanes(ready={"eng": 3, "ci": 1}, live={"eng": 1, "ci": 2}, room=4)
    (text,) = run(store, lanes=full)
    assert caps(store) == (2, 2)
    assert "the ci lane keeps a seat for each of its 2 live agents" in text
    (item,) = master_items(store)
    assert "claim wait" in item.text and "5 task hours" in item.text and "2 threshold" in item.text
    assert recorded(store)["claim_wait"]["text"] == text


def test_one_action_per_stage_per_window_and_the_cap_holds_across_windows(store, home, shipped):
    fire(store, "claim_wait")
    run(store)
    assert run(store, at=NOW + HOUR) == []
    assert caps(store) == (3, 1)
    one_live = lane_split.Lanes(ready={"eng": 3, "ci": 0}, live={"eng": 1, "ci": 1}, room=4)
    later = NOW + bottleneck.WINDOW_MS
    fire(store, "claim_wait", at=later)
    (text,) = run(store, lanes=one_live, at=later)
    assert caps(store) == (3, 1)
    assert "keeps a seat for each of its 1 live agents" in text
    assert len(master_items(store)) == 1


def test_launch_failure_moves_the_pinned_lane_to_the_other_harness(store, home, shipped):
    pin(store, "eng", "claude")
    fire(store, "launch_failure", value=3.0, threshold=1.0)
    (text,) = run(store)
    lane = store.config(SLUG).lanes["eng"]
    assert (lane["agent"], lane["model"]) == ("codex", "auto")
    assert "Moved the eng lane from claude to codex" in text
    assert "launch failure at 3 task hours in the window, over its 1 threshold" in text
    assert recorded(store)["launch_failure"]["text"] == text
    assert master_items(store) == []


def test_quota_hold_with_no_pinned_lane_proposes_one_task(store, home, shipped):
    fire(store, "quota_hold")
    (text,) = run(store)
    assert "no lane is pinned to one harness" in text
    assert len(master_items(store)) == 1
    assert store.config(SLUG).lanes.get("eng", {}).get("agent", "auto") == "auto"


def test_a_stage_with_a_fix_task_raises_it_to_urgent(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    (text,) = run(store, ledger=ledger, doc={"tasks": [fix_task()]})
    assert ledger.ranks == [(SLUG, "r1", "urgent", "dispatcher", True)]
    assert "Raised task r1 to urgent" in text and "review at 5 task hours" in text
    assert recorded(store)["review"]["text"] == text
    assert master_items(store) == []


def test_an_urgent_fix_task_needs_no_further_action(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    assert run(store, ledger=ledger, doc={"tasks": [fix_task(rank="urgent")]}) == []
    assert ledger.ranks == [] and master_items(store) == [] and recorded(store) == {}


def test_a_done_fix_task_is_no_fix_and_the_stage_is_proposed(store, home, shipped):
    fire(store, "review")
    ledger = FakeLedger()
    run(store, ledger=ledger, doc={"tasks": [fix_task(state="done")]})
    assert ledger.ranks == [] and len(master_items(store)) == 1


def test_a_refused_rank_proposes_instead(store, home, shipped):
    fire(store, "review")
    (text,) = run(store, ledger=FakeLedger(refuse=True), doc={"tasks": [fix_task()]})
    assert "the ledger kept the rank of task r1" in text
    assert len(master_items(store)) == 1


def test_a_non_mechanical_stage_sends_exactly_one_proposal_per_window(store, home, shipped):
    fire(store, "merge_queue", value=4.0, threshold=1.5)
    (text,) = run(store)
    assert run(store, at=NOW + HOUR) == []
    (item,) = master_items(store)
    assert "merge queue at 4 task hours in the window, over its 1.5 threshold" in item.text
    assert "Bottleneck fix for the merge queue stage" in item.text
    assert recorded(store) == {"merge_queue": {"text": text, "at": NOW}}
    (row,) = gate_log.recent(SLUG, None, home)
    assert row["kind"] == "propose"
    later = NOW + bottleneck.WINDOW_MS
    fire(store, "merge_queue", at=later)
    assert len(run(store, at=later)) == 1
    assert len(master_items(store)) == 2


def test_below_delegate_a_mechanical_stage_is_proposed(store, home, shipped):
    store.update(SLUG, autonomy=ASSIST)
    fire(store, "claim_wait")
    (text,) = run(store)
    assert caps(store) == (2, 2)
    assert "autonomy" in text
    assert len(master_items(store)) == 1


def test_a_stale_finding_runs_no_remedy(store, home, shipped):
    fire(store, "claim_wait", at=NOW - standing.STALE_MS)
    assert run(store) == []
    assert caps(store) == (2, 2)


def test_the_dispatcher_queue_puts_an_urgent_fix_task_first_and_ignores_its_lower_ranks():
    common = {"state": "open", "lane": "eng", "phase": "p1", "depends_on": []}
    tasks = [
        {"id": "a1", "title": "a", **common},
        {"id": "h1", "title": "h", "rank": "high", **common},
        {"id": "r1", "title": "Bottleneck fix for the review stage", "rank": "urgent", **common},
    ]
    stamps = {f"tasks/{task_id}/rank": {"by": "dispatcher"} for task_id in ("h1", "r1")}
    doc = {"overview": "o", "tasks": tasks, "phases": [{"id": "p1", "depends_on": []}], "_meta": {"stamps": stamps}}
    assert dispatcher.queue(doc, None) == ["r1", "a1", "h1"]
