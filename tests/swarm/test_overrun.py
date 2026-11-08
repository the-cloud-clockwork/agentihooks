from dataclasses import replace
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


class Ledger:
    def __init__(self, task):
        self.rows = {"t1": {"id": "t1", "state": "claimed", "claimed_by": OWNER, "branch": "b1", **task}}
        self.comments, self.updates = [], []

    def state(self, slug):
        return {"tasks": list(self.rows.values()), "phases": [], "_meta": {"events": []}}

    def task(self, slug, task_id):
        assert slug == SLUG
        return self.rows.get(task_id)

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        assert slug == SLUG
        self.updates.append((task_id, fields, if_state))
        row = self.rows[task_id]
        if not if_state or row["state"] in if_state:
            row.update(fields)
        return dict(row)

    def comment(self, slug, task, text, by):
        assert slug == SLUG
        self.comments.append((task, text, by))


class Swarm:
    def __init__(self, task=None, live=True, retires=True, agent=None):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 1, 1))
        self.ledger = Ledger(task or {})
        self.retired, self.retires = [], retires
        self.live = {OWNER} if live else set()
        self.store.put_agent(SLUG, agent or AgentRecord(OWNER, "eng", "t1", seat=SEAT, started_at=START))
        self.store.redis.set(self.store.key(SLUG, "claim", "t1"), OWNER)

    def retire(self, agent):
        self.retired.append(agent.name)
        return self.retires

    def tick(self, at):
        watch = overrun.Watch(self.store, SLUG, self.ledger, frozenset(self.live), self.retire, at)
        return overrun.overrun_pass(watch, self.ledger.state(SLUG))

    def master(self):
        return InboxStore(self.store.redis).inbox("master@sw")

    def agent_inbox(self):
        return InboxStore(self.store.redis).inbox(OWNER)

    def working(self):
        return [a.name for a in self.store.agents(SLUG) if a.state != "finished"]


def _progress(lines):
    folder = Path.home() / ".agentihooks" / "swarm" / SLUG / "tasks" / "t1"
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


@pytest.mark.feature("hive-overrun-progress")
@pytest.mark.parametrize("local", ["", "stale workstation step\n"])
def test_remote_overrun_quotes_the_same_progress_as_the_ledger(local: str) -> None:
    import json

    from scripts.swarm import commands
    from scripts.swarm_ledger import ledger_workspace

    _progress(local.splitlines())
    agent = AgentRecord(
        OWNER,
        "eng",
        "t1",
        seat=SEAT,
        started_at=START,
        hive="remote",
        runtime_target={"host_id": "remote"},
    )
    swarm = Swarm(task={"workspace": str(ledger_workspace.folder(SLUG, "t1"))}, agent=agent)
    expected = {
        "latest_progress": "hive step seven\nhive step eight\nhive step nine",
        "latest_proof": "hive check green",
    }
    swarm.store.redis.set(ledger_workspace.synced_key("remote"), json.dumps({SLUG: {"t1": expected}}))
    published = ledger_workspace.published(swarm.store, SLUG, swarm.ledger.state(SLUG))
    commands.publish(swarm.store, SLUG, {}, published)
    swarm.tick(REPORT)
    [item] = swarm.master()
    page = commands.workspaces(swarm.store, SLUG)["t1"]
    assert page == expected
    assert f"Last steps: {'; '.join(page['latest_progress'].splitlines())}. " in item.text
    assert "stale workstation step" not in item.text
    assert overrun.NO_STEPS not in item.text


def test_the_limits_are_set_in_code() -> None:
    assert overrun.REPORT_MINUTES == 90


def test_an_agent_under_the_report_limit_is_left_alone():
    swarm = Swarm()
    assert swarm.tick(REPORT - MINUTE) == []
    assert swarm.master() == [] and swarm.agent_inbox() == []


def test_past_the_report_limit_the_master_hears_the_task_its_age_and_last_steps_once() -> None:
    _progress(["opened the issue", "pushed the red test", "fixed the push guard", "waiting on the queue"])
    swarm = Swarm()
    actions = swarm.tick(REPORT)
    [item] = swarm.master()
    assert "t1" in item.text and OWNER in item.text and " 90 minutes " in item.text
    assert "Last steps: pushed the red test; fixed the push guard; waiting on the queue. " in item.text
    assert item.text.startswith("Overrun on swarm sw: ") and " Only its context limit hands it off. " in item.text
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


def test_a_pull_request_task_counts_as_claimed() -> None:
    swarm = Swarm(task={"state": "pr"})
    assert len(swarm.tick(REPORT)) == 1 and len(swarm.master()) == 1


def test_outage_time_does_not_count_toward_the_age(monkeypatch):
    monkeypatch.setattr(
        overrun.outage,
        "paused_ms",
        lambda slug, since, now: 30 * MINUTE if (slug, since) == (SLUG, START) and now > since else 0,
    )
    swarm = Swarm()
    assert swarm.tick(REPORT + 29 * MINUTE) == []
    assert len(swarm.tick(REPORT + 30 * MINUTE)) == 1


def test_an_outage_holds_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        overrun.outage, "held", lambda store, slug, name: "probe failed" if (slug, name) == (SLUG, OWNER) else ""
    )
    swarm = Swarm()
    assert swarm.tick(REPORT + 60 * MINUTE) == []
    assert swarm.master() == [] and swarm.retired == []


def test_a_master_an_agent_off_its_claim_or_a_dead_pane_is_skipped() -> None:
    for swarm in (
        Swarm(agent=AgentRecord(OWNER, MASTER, "t1", started_at=START)),
        Swarm(task={"claimed_by": "engineer@abc-0002"}),
        Swarm(task={"state": "open"}),
        Swarm(live=False),
        Swarm(agent=AgentRecord(OWNER, "eng", "t1", seat=SEAT)),
        Swarm(agent=AgentRecord(OWNER, "eng", "", seat=SEAT, started_at=START)),
    ):
        assert swarm.tick(REPORT + 60 * MINUTE) == []
        assert swarm.master() == [] and swarm.retired == []


def test_a_new_life_on_the_same_task_starts_its_own_clock() -> None:
    swarm = Swarm()
    swarm.tick(REPORT)
    swarm.store.drop_agent(SLUG, OWNER)
    successor = "engineer@abc-0002"
    swarm.store.put_agent(SLUG, AgentRecord(successor, "eng", "t1", seat=SEAT, started_at=REPORT))
    swarm.ledger.rows["t1"]["claimed_by"] = successor
    swarm.live = {successor}
    assert swarm.tick(REPORT + MINUTE) == []
    assert swarm.store.redis.hgetall(swarm.store.key(SLUG, overrun.KEY)) == {}


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
    assert seen == [swarm.ledger.state(SLUG)]


def test_a_planner_is_watched_like_an_engineer() -> None:
    swarm = Swarm(agent=AgentRecord(OWNER, "plan", "t1", seat=SEAT, started_at=START))
    assert len(swarm.tick(REPORT)) == 1 and len(swarm.master()) == 1


def test_an_agent_released_through_ready_is_never_reported() -> None:
    swarm = Swarm(task={"state": "pr", "awaiting": "merge"})
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, replace(agent, state="finished"))
    assert swarm.tick(REPORT) == [] and swarm.master() == []


def test_a_mark_left_from_another_task_starts_over() -> None:
    swarm = Swarm()
    swarm.tick(REPORT)
    [agent] = swarm.store.agents(SLUG)
    swarm.store.put_agent(SLUG, replace(agent, task="t2"))
    swarm.ledger.rows["t2"] = {**swarm.ledger.rows["t1"], "id": "t2"}
    assert len(swarm.tick(REPORT + MINUTE)) == 1 and len(swarm.master()) == 2


@pytest.mark.feature("overrun-no-wall-time-handoff")
def test_a_live_agent_at_two_hundred_minutes_gets_one_report_and_no_ask_or_end() -> None:
    swarm = Swarm()
    actions = [action for at in range(START, START + 201 * MINUTE, MINUTE) for action in swarm.tick(at)]
    assert actions == [f"overrun: told master@sw that {OWNER} has run 90 minutes on task t1"]
    assert len(swarm.master()) == 1 and swarm.agent_inbox() == []
    assert swarm.retired == [] and swarm.working() == [OWNER]
    assert swarm.ledger.rows["t1"]["claimed_by"] == OWNER and swarm.ledger.comments == []


@pytest.mark.parametrize("state", ["claimed", "pr"])
@pytest.mark.parametrize("minutes", [150, 165, 200, 300, 600, 1440, 4320])
def test_no_wall_time_age_asks_or_ends_a_live_agent(state: str, minutes: int) -> None:
    swarm = Swarm(task={"state": state})
    at = START + minutes * MINUTE
    actions = swarm.tick(at) + swarm.tick(at + overrun.REPORT_MINUTES * MINUTE)
    assert actions == [f"overrun: told master@sw that {OWNER} has run {minutes} minutes on task t1"]
    assert swarm.agent_inbox() == [] and swarm.retired == [] and swarm.working() == [OWNER]
    assert swarm.ledger.rows["t1"]["claimed_by"] == OWNER and swarm.ledger.updates == []
