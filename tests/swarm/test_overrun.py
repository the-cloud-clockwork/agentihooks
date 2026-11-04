from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import overrun
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

SLUG = "sw"
OWNER = "engineer@abc-0001"
SEAT = "eng-1@sw"
MINUTE = 60_000
START = 10_000_000
REPORT_MS = overrun.REPORT_MINUTES * MINUTE
REPORT = START + REPORT_MS
FAR_PAST = REPORT + 100_000 * MINUTE


class Ledger:
    def __init__(self, task):
        self.rows = {"t1": {"id": "t1", "state": "claimed", "claimed_by": OWNER, "branch": "b1", **task}}


class Swarm:
    def __init__(self, task=None, live=True, agent=None):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 1, 1))
        self.ledger = Ledger(task or {})
        self.live = {OWNER} if live else set()
        self.store.put_agent(SLUG, agent or AgentRecord(OWNER, "eng", "t1", seat=SEAT, started_at=START))
        self.store.redis.set(self.store.key(SLUG, "claim", "t1"), OWNER)

    def tick(self, at):
        watch = overrun.Watch(self.store, SLUG, frozenset(self.live), at)
        return overrun.overrun_pass(watch, {"tasks": list(self.ledger.rows.values())})

    def master(self):
        return InboxStore(self.store.redis).inbox("master@sw")

    def agent_inbox(self):
        return InboxStore(self.store.redis).inbox(OWNER)


def _progress(lines):
    folder = Path.home() / ".agentihooks" / "swarm" / SLUG / "tasks" / "t1"
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_the_report_limit_is_set_in_code():
    assert overrun.REPORT_MINUTES == 90


def test_an_agent_under_the_report_limit_is_left_alone():
    swarm = Swarm()
    assert swarm.tick(REPORT - MINUTE) == []
    assert swarm.master() == [] and swarm.agent_inbox() == []


def test_past_the_report_limit_the_master_hears_the_task_its_age_and_last_steps_once():
    _progress(["opened the issue", "pushed the red test", "fixed the push guard", "waiting on the queue"])
    swarm = Swarm()
    actions = swarm.tick(REPORT)
    [item] = swarm.master()
    assert "t1" in item.text and OWNER in item.text and " 90 minutes " in item.text
    assert "Last steps: pushed the red test; fixed the push guard; waiting on the queue. " in item.text
    assert item.text.startswith("Overrun on swarm sw: ") and item.text.endswith("Close this item once you have looked.")
    assert (item.ref, item.task) == ("sw:overrun:t1", "t1")
    assert actions == [f"overrun: told master@sw that {OWNER} has run 90 minutes on task t1"]
    assert swarm.agent_inbox() == []
    assert swarm.tick(REPORT + 10 * MINUTE) == []
    assert len(swarm.master()) == 1


def test_a_report_without_progress_lines_says_none_were_recorded():
    swarm = Swarm()
    swarm.tick(REPORT)
    [item] = swarm.master()
    assert overrun.NO_STEPS in item.text


@pytest.mark.feature("overrun-no-wall-time-handoff")
def test_an_agent_far_past_the_report_limit_is_only_ever_reported_never_asked_or_retired():
    swarm = Swarm()
    actions = swarm.tick(FAR_PAST)
    assert actions == [
        f"overrun: told master@sw that {OWNER} has run {(FAR_PAST - START) // MINUTE} minutes on task t1"
    ]
    [item] = swarm.master()
    assert "hand off" not in item.text.lower() and "handoff" not in item.text.lower()
    assert "ended" not in item.text.lower() and "retire" not in item.text.lower()
    assert swarm.agent_inbox() == []
    assert swarm.ledger.rows["t1"]["state"] == "claimed" and swarm.ledger.rows["t1"]["claimed_by"] == OWNER
    assert swarm.tick(FAR_PAST + 10 * MINUTE) == []
    assert len(swarm.master()) == 1


def test_outage_time_does_not_count_toward_the_age(monkeypatch):
    monkeypatch.setattr(
        overrun.outage,
        "paused_ms",
        lambda slug, since, now: 30 * MINUTE if (slug, since) == (SLUG, START) and now > since else 0,
    )
    swarm = Swarm()
    assert swarm.tick(REPORT + 29 * MINUTE) == []
    assert len(swarm.tick(REPORT + 30 * MINUTE)) == 1


def test_an_outage_holds_every_step(monkeypatch):
    monkeypatch.setattr(
        overrun.outage, "held", lambda store, slug, name: "probe failed" if (slug, name) == (SLUG, OWNER) else ""
    )
    swarm = Swarm()
    assert swarm.tick(FAR_PAST) == []
    assert swarm.master() == []


@pytest.mark.parametrize(
    "setup",
    [
        {"agent": AgentRecord(OWNER, MASTER, "t1", started_at=START)},
        {"task": {"claimed_by": "engineer@abc-0002"}},
        {"task": {"state": "open"}},
        {"live": False},
        {"agent": AgentRecord(OWNER, "eng", "t1", seat=SEAT)},
        {"agent": AgentRecord(OWNER, "eng", "", seat=SEAT, started_at=START)},
    ],
    ids=["a-master", "off-its-claim", "task-open", "dead-pane", "no-seat", "no-task"],
)
def test_a_master_an_agent_off_its_claim_or_a_dead_pane_is_skipped(setup):
    swarm = Swarm(**setup)
    assert swarm.tick(FAR_PAST) == []
    assert swarm.master() == []


def test_a_report_names_whichever_task_and_agent_is_overrunning():
    swarm = Swarm(agent=AgentRecord("engineer@abc-0005", "eng", "t5", seat=SEAT, started_at=START))
    swarm.ledger.rows["t5"] = {"id": "t5", "state": "claimed", "claimed_by": "engineer@abc-0005"}
    swarm.live = {"engineer@abc-0005"}
    [action] = swarm.tick(FAR_PAST)
    assert "t5" in action and "engineer@abc-0005" in action


def test_a_claim_closed_between_ticks_stops_further_watching():
    swarm = Swarm()
    swarm.tick(REPORT)
    swarm.ledger.rows["t1"]["state"] = "done"
    assert swarm.tick(FAR_PAST) == []
    assert len(swarm.master()) == 1


def test_the_report_item_carries_its_task_id():
    swarm = Swarm()
    swarm.tick(REPORT)
    [item] = swarm.master()
    assert item.task == "t1" and item.ref == "sw:overrun:t1"


def test_the_park_and_grace_minutes_constants_are_gone():
    assert not hasattr(overrun, "PARK_MINUTES")
    assert not hasattr(overrun, "GRACE_MINUTES")
    assert not hasattr(overrun, "ASK")
    assert not hasattr(overrun, "ENDED")
    assert not hasattr(overrun, "COMMENT")
    assert not hasattr(overrun, "ASK_CLOSED")


def test_a_new_life_on_the_same_task_starts_its_own_clock():
    swarm = Swarm()
    swarm.tick(REPORT)
    swarm.store.drop_agent(SLUG, OWNER)
    successor = "engineer@abc-0002"
    swarm.store.put_agent(SLUG, AgentRecord(successor, "eng", "t1", seat=SEAT, started_at=REPORT))
    swarm.ledger.rows["t1"]["claimed_by"] = successor
    swarm.live = {successor}
    assert swarm.tick(REPORT + MINUTE) == []
    assert len(swarm.tick(REPORT + REPORT_MS + MINUTE)) == 1


def test_one_tick_reports_every_overrunning_agent_past_a_finished_one():
    swarm = Swarm(agent=AgentRecord("engineer@abc-0000", "eng", "t0", state="finished", started_at=START))
    others = ("engineer@abc-0002", "engineer@abc-0003")
    for name, task in ((OWNER, "t1"), *zip(others, ("t2", "t3"), strict=True)):
        swarm.store.put_agent(SLUG, AgentRecord(name, "eng", task, seat=SEAT, started_at=START))
        swarm.ledger.rows[task] = {"id": task, "state": "claimed", "claimed_by": name}
    swarm.live = {OWNER, *others}
    assert len(swarm.tick(REPORT)) == 3 and len(swarm.master()) == 3


def test_an_agent_started_in_the_first_millisecond_is_still_watched():
    swarm = Swarm(agent=AgentRecord(OWNER, "eng", "t1", seat=SEAT, started_at=1))
    assert len(swarm.tick(1 + REPORT_MS)) == 1


def test_the_report_routes_through_the_ledger_document(monkeypatch):
    seen, real = [], overrun.Mail
    monkeypatch.setattr(
        overrun, "Mail", lambda inbox, store, slug, doc: seen.append(doc) or real(inbox, store, slug, doc)
    )
    swarm = Swarm()
    swarm.tick(REPORT)
    assert seen == [{"tasks": list(swarm.ledger.rows.values())}]


def test_a_planner_is_watched_like_an_engineer():
    swarm = Swarm(agent=AgentRecord(OWNER, "plan", "t1", seat=SEAT, started_at=START))
    assert len(swarm.tick(FAR_PAST)) == 1
    assert swarm.tick(FAR_PAST + 10 * MINUTE) == []


def test_an_agent_released_through_ready_is_still_reported():
    swarm = Swarm(task={"state": "pr", "awaiting": "merge"})
    assert len(swarm.tick(REPORT)) == 1


def test_a_pull_request_task_counts_as_claimed_and_is_still_reported():
    swarm = Swarm(task={"state": "pr"})
    assert swarm.tick(REPORT) == [f"overrun: told master@sw that {OWNER} has run 90 minutes on task t1"]
    assert swarm.tick(REPORT + MINUTE) == []


def test_a_mark_left_from_another_task_starts_over():
    swarm = Swarm()
    swarm.tick(REPORT)
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, AgentRecord(agent.name, agent.lane, "t2", seat=agent.seat, started_at=agent.started_at))
    swarm.ledger.rows["t2"] = {**swarm.ledger.rows["t1"], "id": "t2"}
    actions = swarm.tick(FAR_PAST)
    assert len(actions) == 1
    assert len(swarm.master()) == 2
