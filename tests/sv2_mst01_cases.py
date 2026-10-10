import hashlib
import json
from pathlib import Path

from scripts.inbox.store import InboxStore
from scripts.swarm import operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.watch_ledger import line
from scripts.swarm_v2 import masters

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/master-seats.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = INPUTS["slug"]
EVIDENCE_CLASS = "fakeredis swarm store and inbox; fixture ledger document; no live swarm"
DOC = {
    "phases": [{"id": p, "title": f"phase {p}"} for p in INPUTS["phases"]],
    "tasks": INPUTS["tasks"],
    "_meta": {"rev": 1, "events": []},
}


class World:
    def __init__(self):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 1, 1))
        self.inbox = InboxStore(self.store.redis)
        self.seats = masters.MasterSeats(self.store.redis)
        self.seats.set_count(SLUG, INPUTS["master_count"])
        self.addresses = masters.seats(SLUG, 3)

    def seat(self, *indexes):
        for index in indexes:
            agent = AgentRecord(
                f"{SLUG}-master-{index}", MASTER, MASTER, pane_id=f"pm{index}", seat=self.addresses[index - 1]
            )
            self.store.put_agent(SLUG, agent)
            self.store.seats.occupy(agent.seat, agent.name, 1)

    def relay(self, rev, target, text):
        event = {
            "rev": rev,
            "at": rev,
            "by": "operator",
            "kind": "comment added",
            "target": target,
            "id": f"c{rev}",
            "text": text,
        }
        operator_mail.relay(self.inbox, self.store, SLUG, DOC, [event], line)

    def pending(self):
        return {address: len(self.inbox.pending_items(address)) for address in self.addresses}


def _refusal(act):
    try:
        act()
    except masters.MasterError as refused:
        return str(refused)
    return None


def _positive(world):
    world.seat(1, 2)
    owners = world.seats.owners(SLUG, DOC)
    world.relay(2, "tasks/t2", "please look at this task")
    on_task = world.pending()
    world.relay(3, "chat", "where are we")
    return {"owners": owners, "after_task_comment": on_task, "after_unaddressed_chat": world.pending()}


def _rejection(world):
    world.seat(1)
    owners = world.seats.owners(SLUG, DOC)
    world.relay(2, "phases/p2", "this phase owner has no live master")
    return {
        "owners": owners,
        "after_comment_on_empty_owner_seat": world.pending(),
        "count_zero": _refusal(lambda: world.seats.set_count(SLUG, 0)),
        "count_text": _refusal(lambda: masters.count_of("two")),
        "count_kept": world.seats.count(SLUG),
    }


def _recovery(world):
    world.seats.set_count(SLUG, 3)
    three = world.seats.owners(SLUG, DOC)
    world.seats.set_count(SLUG, 2)
    two = world.seats.owners(SLUG, DOC)
    restarted = masters.MasterSeats(world.store.redis).owners(SLUG, {**DOC, "phases": DOC["phases"][::-1]})
    return {
        "owners_with_three_seats": three,
        "owners_after_removing_seat_three": two,
        "moved": sorted(p for p in three if three[p] != two[p]),
        "owners_after_restart": dict(sorted(restarted.items())),
    }


def run_case(case):
    observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](World())
    return {
        "case": f"T-SV2-MST-01-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "observed": observed,
    }
