import hashlib
import json
import re
from argparse import Namespace
from collections import Counter
from dataclasses import replace
from pathlib import Path

from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import master_scale, masters
from scripts.swarm_v2.masters_channel import MastersChannel
from tests.swarm.test_tick import FakeRuntime

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/master-scaling.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = INPUTS["slug"]
EVIDENCE_CLASS = (
    "fakeredis swarm store, inbox and masters channel; fake herdr runtime; fixture ledger document; no live swarm"
)
DOC = {
    "phases": [{"id": p, "title": f"phase {p}"} for p in INPUTS["phases"]],
    "tasks": [],
    "_meta": {"rev": 1, "events": []},
}
NOW = 1_800_000_000_000
MINUTE = 60_000
LEAD, SECOND, THIRD, FOURTH = masters.seats(SLUG, 4)


class World:
    def __init__(self):
        import fakeredis

        self.server = fakeredis.FakeServer()
        self.store = RedisStore(fakeredis.FakeRedis(server=self.server, decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 20, 0))
        self.store.update(SLUG, state="running")
        master_scale.set_rule(self.store.redis, SLUG, master_scale.rule_of(INPUTS["rule"]))
        self.runtime = FakeRuntime()
        self.inbox = InboxStore(self.store.redis)
        lead = f"{SLUG}-master-1"
        self.store.put_agent(SLUG, AgentRecord(lead, MASTER, MASTER, pane_id="pm1", seat=LEAD))
        self.store.seats.occupy(LEAD, lead, 1)
        self.runtime.live.add(lead)

    def staffed(self, size):
        for agent in self.store.agents(SLUG):
            if agent.lane != MASTER:
                self.store.drop_agent(SLUG, agent.name)
        for i in range(size):
            self.store.put_agent(SLUG, AgentRecord(f"{SLUG}-eng-{i}", "eng", f"t{i}", seat=f"eng-{i}@{SLUG}"))

    def tick(self, now):
        actions = master_scale.run(SLUG, self.store.config(SLUG), self.store, self.runtime, DOC, now)
        return [re.sub(r"^(spawned master|asked) \S+ ", r"\1 ", action) for action in actions]

    def extras(self):
        found = [a for a in self.store.agents(SLUG) if a.lane == MASTER and a.seat != LEAD and a.state != "finished"]
        return sorted(found, key=lambda a: a.seat)

    def count(self):
        return masters.MasterSeats(self.store.redis).count(SLUG)

    def phases_per_seat(self):
        return dict(sorted(Counter(masters.MasterSeats(self.store.redis).owners(SLUG, DOC).values()).items()))

    def announcements(self):
        return [post.text for post in MastersChannel(self.store.redis).read(SLUG, f"{SLUG}-master-1")]

    def pending(self, address):
        return [item.sender for item in self.inbox.pending_items(address)]

    def hand_off(self, agent):
        self.store.put_handoff(SLUG, agent.task, f"# Handoff v2\nnotes of {agent.seat}", seat=agent.seat)
        self.store.put_agent(SLUG, replace(agent, state="finished"))
        self.runtime.live.discard(agent.name)

    def grow(self):
        self.staffed(5)
        at_five = self.tick(NOW)
        self.staffed(20)
        return at_five, self.tick(NOW + MINUTE)


def _refusal(act):
    try:
        act()
    except masters.MasterError as refused:
        return str(refused)
    return None


def _positive(world):
    at_five, at_twenty = world.grow()
    again = world.tick(NOW + 2 * MINUTE)
    return {
        "actions_at_five": at_five,
        "actions_at_twenty": at_twenty,
        "actions_next_tick": again,
        "count": world.count(),
        "launched": [[a.seat, a.task] for a in world.extras()],
        "launches": len(world.runtime.masters),
        "phases_per_seat": world.phases_per_seat(),
        "announcements": world.announcements(),
    }


def _negative(world):
    world.grow()
    world.staffed(5)
    start = NOW + 2 * MINUTE
    inside = world.tick(start) + world.tick(start + master_scale.HOLD_MS - 1)
    count_inside = world.count()
    asks = world.tick(start + master_scale.HOLD_MS)
    repeat = world.tick(start + master_scale.HOLD_MS + MINUTE)
    return {
        "unknown_rule": _refusal(lambda: cli.cmd_set(world.store, Namespace(slug=SLUG, pairs=["master-per=lots"]))),
        "rule_kept": master_scale.rule(world.store.redis, SLUG),
        "actions_inside_hold": inside,
        "count_inside_hold": count_inside,
        "actions_after_hold": asks,
        "actions_with_live_extras": repeat,
        "count_with_live_extras": world.count(),
        "asks_per_seat": {a.seat: world.pending(a.seat) for a in world.extras()},
    }


def _recovery(world):
    import fakeredis

    world.grow()
    world.staffed(5)
    start = NOW + 2 * MINUTE
    world.tick(start)
    world.tick(start + master_scale.HOLD_MS)
    question = world.inbox.send(f"{SLUG}-eng-1", FOURTH, "a question about the last phase")
    for agent in world.extras():
        world.hand_off(agent)
    retired = world.tick(start + master_scale.HOLD_MS + MINUTE)
    restarted = masters.MasterSeats(fakeredis.FakeRedis(server=world.server, decode_responses=True))
    lead_items = world.inbox.pending_items(LEAD)
    return {
        "actions": retired,
        "count": world.count(),
        "phases_per_seat": world.phases_per_seat(),
        "handoffs_at_lead": sorted(
            seat for seat in (SECOND, THIRD, FOURTH) if any(f"notes of {seat}" in i.text for i in lead_items)
        ),
        "question_at": world.inbox.get(question.id).address,
        "open_at_retired_seats": sum(len(world.inbox.open_items(a)) for a in (SECOND, THIRD, FOURTH)),
        "owners_after_restart_match": restarted.owners(SLUG, DOC)
        == masters.MasterSeats(world.store.redis).owners(SLUG, DOC),
        "last_announcement": world.announcements()[-1],
        "measurements": master_scale.measurements(world.store.redis, SLUG),
    }


def _meets_positive(o):
    grown = o["actions_at_five"] == [] and o["actions_next_tick"] == [] and o["count"] == 4
    launched = o["launched"] == [[SECOND, "master-2"], [THIRD, "master-3"], [FOURTH, "master-4"]]
    split = o["phases_per_seat"] == {LEAD: 4, SECOND: 4, THIRD: 4, FOURTH: 4}
    return grown and launched and o["launches"] == 3 and split and len(o["announcements"]) == 1


def _meets_negative(o):
    refused = o["unknown_rule"] is not None and o["rule_kept"] == INPUTS["rule"]
    held = o["actions_inside_hold"] == [] and o["count_inside_hold"] == 4
    asked = len(o["actions_after_hold"]) == 3 and o["actions_with_live_extras"] == []
    once = all(senders == ["swarm"] for senders in o["asks_per_seat"].values()) and len(o["asks_per_seat"]) == 3
    return refused and held and asked and once and o["count_with_live_extras"] == 4


def _meets_recovery(o):
    back = o["count"] == 1 and o["phases_per_seat"] == {LEAD: len(INPUTS["phases"])}
    moved = o["handoffs_at_lead"] == [SECOND, THIRD, FOURTH] and o["question_at"] == LEAD
    measured = o["measurements"]["master_seat_count"] == 1 and len(o["measurements"]["master_retire_seconds"]) == 3
    return back and moved and o["open_at_retired_seats"] == 0 and o["owners_after_restart_match"] and measured


SPEC = {"a": _meets_positive, "b": _meets_negative, "c": _meets_recovery}


def run_case(case):
    observed = {"a": _positive, "b": _negative, "c": _recovery}[case](World())
    return {
        "case": f"T-SV2-MST-03-{case.upper()}",
        "state": "passed" if SPEC[case](observed) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "observed": observed,
    }
