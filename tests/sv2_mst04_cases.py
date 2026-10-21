import hashlib
import json
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import scripts.swarm_ledger.repository  # noqa: F401  puts the ledger folder on sys.path before ledger_core loads
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import new_ledger
from scripts.swarm_v2 import master_fence, masters
from scripts.swarm_v2.masters_channel import MastersChannel
from tests.swarm_ledger import legacy_page

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/master-race.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = INPUTS["slug"]
LEAD, SECOND = masters.seats(SLUG, 2)
LEAD_NAME, SECOND_NAME, FORMER, ENGINEER = INPUTS["lead"], INPUTS["second"], INPUTS["former"], INPUTS["engineer"]
SUCCESSOR = "master@abcdef-0003"
FOLLOWUP = f"followups/{INPUTS['followup']['id']}"
QUESTION = f"questions/{INPUTS['question']['id']}"
EVIDENCE_CLASS = (
    "fakeredis swarm store, seats and masters channel; a temporary ledger folder written through the ledger write "
    "path with the master fence; no live swarm or ledger server"
)


class World:
    """A ledger with two phases, a task in each, one follow up and one question, and two live master seats."""

    def __init__(self):
        import fakeredis

        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.store = RedisStore(self.redis)
        self.store.create(SwarmConfig(SLUG, "/repo", 2, 0))
        masters.MasterSeats(self.redis).set_count(SLUG, 2)
        self.seat(LEAD_NAME, LEAD)
        self.seat(SECOND_NAME, SECOND)
        content = {
            "title": "Race",
            "overview": "Two masters race on one follow up",
            "sources": [],
            "phases": [{"title": title, "description": title} for title in INPUTS["phases"]],
        }
        html_path, _ = legacy_page.paths(SLUG, core)
        html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
        core.sync(SLUG)
        ops = [{"op": "join", "id": f"join-{n}", "by": n, "role": "orchestrator"} for n in (LEAD_NAME, SECOND_NAME)]
        ops += [
            {"op": "task_add", "id": f"add-{t['id']}", "by": "swarm", "task": t["id"], "title": t["title"]}
            | {"lane": "eng", "phase": t["phase"]}
            for t in INPUTS["tasks"]
        ]
        for name, row in (("followups", INPUTS["followup"]), ("questions", INPUTS["question"])):
            ops.append({"op": "add_item", "id": row["id"], "by": ENGINEER, "list": name, "text": row["text"]})
        core.sync(SLUG, ops=ops)

    def seat(self, name, address):
        self.store.put_agent(SLUG, AgentRecord(name, MASTER, MASTER, pane_id=f"p-{name}", seat=address))
        self.store.seats.occupy(address, name, 1)
        if address == LEAD:
            self.lead = name

    def down(self, name):
        agent = next(a for a in self.store.agents(SLUG) if a.name == name)
        self.store.put_agent(SLUG, replace(agent, state="finished"))

    def write(self, op, connect=None):
        fence = master_fence.Fence(SLUG, connect=connect or (lambda: self.redis))
        state, rejected = core.sync(SLUG, ops=[op], gate=fence)
        fence.publish(rejected)
        return {"accepted": op["id"] not in rejected, "warnings": state["_meta"]["warnings"]}

    def decide(self, by, value=True, n=0):
        path = f"{FOLLOWUP}/done"
        return self.write({"op": "set", "id": f"set-{by}-{n}", "by": by, "path": path, "value": value})

    def answer(self, by):
        return self.write({"op": "answer", "id": f"ans-{by}", "by": by, "item": QUESTION, "text": "Use the spare one"})

    def set_task(self, by, task):
        fields = {"description": f"Updated by {by}"}
        return self.write(
            {"op": "task_update", "id": f"upd-{by}-{task}", "by": by, "item": f"tasks/{task}"} | {"fields": fields}
        )

    def operator_sets(self, value):
        core.sync(SLUG, changes=[{"path": f"{FOLLOWUP}/done", "value": value}])

    def race(self):
        start, results = threading.Barrier(INPUTS["racers"]), {}

        def run(name):
            start.wait()
            results[name] = self.decide(name)

        threads = [threading.Thread(target=run, args=(name,)) for name in (LEAD_NAME, SECOND_NAME)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def doc(self):
        return core.sync(SLUG)[0]

    def followup(self):
        return next(f for f in self.doc()["followups"] if f["id"] == INPUTS["followup"]["id"])

    def decisions(self):
        return self.doc()["_meta"].get(master_fence.DECISIONS, {})

    def cross(self):
        return self.doc()["_meta"].get(master_fence.CROSS, {})

    def posts(self):
        return [post.text for post in MastersChannel(self.redis).read(SLUG, self.lead)]

    def refusals(self):
        return master_fence.measurements(self.redis, SLUG)["master_write_refusals_total"]


def _recorded(world):
    return {
        "deciders": {item: row["seat"] for item, row in world.decisions().items()},
        "cross": {item: [row["owner"], row["seat"], row["by"]] for item, row in world.cross().items()},
    }


def _positive(world):
    accepted = {
        "decide": world.decide(SECOND_NAME)["accepted"],
        "answer": world.answer(SECOND_NAME)["accepted"],
        "set_task": world.set_task(SECOND_NAME, "t1")["accepted"],
        "own_task": world.set_task(SECOND_NAME, "t2")["accepted"],
    }
    doc = world.doc()
    return {
        "accepted": accepted,
        "followup_done": world.followup()["done"],
        "answered": len(next(q["answers"] for q in doc["questions"] if q["id"] == INPUTS["question"]["id"])),
        "t1_description": next(t.get("description", "") for t in doc["tasks"] if t["id"] == "t1"),
        **_recorded(world),
        "posts": world.posts(),
        "refusals": world.refusals(),
    }


def _negative(world):
    refused = {
        "decide": world.decide(FORMER)["warnings"],
        "answer": world.answer(FORMER)["warnings"],
        "set_task": world.set_task(FORMER, "t1")["warnings"],
    }
    doc = world.doc()
    world.operator_sets(True)
    return {
        "refused": refused,
        "followup_done": next(f["done"] for f in doc["followups"] if f["id"] == INPUTS["followup"]["id"]),
        "answers": next(q["answers"] for q in doc["questions"] if q["id"] == INPUTS["question"]["id"]),
        "t1_description": next(t.get("description", "") for t in doc["tasks"] if t["id"] == "t1"),
        **_recorded(world),
        "posts": world.posts(),
        "refusals": world.refusals(),
        "operator_write_kept": world.followup()["done"] is True,
    }


def _refused_naming(warnings, owner, writer):
    text = warnings[0] if len(warnings) == 1 else ""
    decided = text.startswith(f"{FOLLOWUP} was decided by {owner} at revision ")
    return decided and text.endswith(f", so {writer} cannot decide it")


def _recovery(world):
    import fakeredis

    world.down(LEAD_NAME)
    by_acting_lead = world.decide(SECOND_NAME)
    world.seat(SUCCESSOR, LEAD)
    successor = world.decide(SUCCESSOR, value=False)
    world.down(SECOND_NAME)
    gone = world.decide(SECOND_NAME, n=1)
    restarted = fakeredis.FakeRedis(server=world.server, decode_responses=True)
    again = world.write(
        {"op": "set", "id": "set-again", "by": SUCCESSOR, "path": f"{FOLLOWUP}/done", "value": True},
        connect=lambda: restarted,
    )
    return {
        "acting_lead_accepted": by_acting_lead["accepted"],
        "successor_accepted": successor["accepted"],
        "gone_second": [gone["accepted"], _refused_naming(gone["warnings"], SECOND, SECOND_NAME)],
        "after_restart_accepted": again["accepted"],
        **_recorded(world),
        "followup_done": world.followup()["done"],
        "posts": world.posts(),
        "refusals": world.refusals(),
    }


def _meets_positive(o):
    accepted = all(o["accepted"].values()) and o["refusals"] == 0
    written = o["followup_done"] is True and o["answered"] == 1 and o["t1_description"] == f"Updated by {SECOND_NAME}"
    owners = o["deciders"] == {FOLLOWUP: LEAD, QUESTION: LEAD}
    by_second = [LEAD, SECOND, SECOND_NAME]
    recorded = o["cross"] == {FOLLOWUP: by_second, QUESTION: by_second, "tasks/t1": by_second}
    posted = o["posts"][-2:] == [f"{SECOND} set tasks/t1: description for {LEAD}", f"{SECOND} set tasks/t2: description"]
    return accepted and written and owners and recorded and posted


def _meets_negative(o):
    expected = {
        "decide": [f"{FOLLOWUP} belongs to {LEAD}, so {FORMER} cannot decide it"],
        "answer": [f"{QUESTION} belongs to {LEAD}, so {FORMER} cannot answer it"],
        "set_task": [f"tasks/t1 belongs to {LEAD}, so {FORMER} cannot set it"],
    }
    unchanged = o["followup_done"] is False and o["answers"] == [] and o["t1_description"] == ""
    quiet = o["deciders"] == {} and o["cross"] == {} and o["posts"] == [] and o["refusals"] == 3
    return o["refused"] == expected and unchanged and quiet and o["operator_write_kept"]


def _meets_recovery(o):
    accepted = o["acting_lead_accepted"] and o["successor_accepted"] and o["after_restart_accepted"]
    kept = o["deciders"] == {FOLLOWUP: SECOND} and o["cross"] == {FOLLOWUP: [SECOND, LEAD, SUCCESSOR]}
    posted = o["posts"] == [
        f"{SECOND} set {FOLLOWUP}/done to true",
        f"{LEAD} set {FOLLOWUP}/done to false for {SECOND}",
        f"{LEAD} set {FOLLOWUP}/done to true for {SECOND}",
    ]
    gone = o["gone_second"] == [False, True]
    return accepted and kept and posted and gone and o["followup_done"] is True and o["refusals"] == 1


SPEC = {"a": _meets_positive, "b": _meets_negative, "c": _meets_recovery}


def run_case(case):
    saved = core.LEDGER_DIR
    with tempfile.TemporaryDirectory() as folder:
        core.LEDGER_DIR = Path(folder)
        try:
            observed = {"a": _positive, "b": _negative, "c": _recovery}[case](World())
        finally:
            core.LEDGER_DIR = saved
    return {
        "case": f"T-SV2-MST-04-{case.upper()}",
        "state": "passed" if SPEC[case](observed) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "observed": observed,
    }
