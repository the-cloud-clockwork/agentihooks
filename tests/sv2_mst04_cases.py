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
        html_path, _ = core.paths(SLUG)
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

    def posts(self):
        return [post.text for post in MastersChannel(self.redis).read(SLUG, self.lead)]

    def refusals(self):
        return master_fence.measurements(self.redis, SLUG)["master_write_refusals_total"]


def names_lead(warnings):
    """Master two loses either before the lead writes (routing) or after (the lead's decider record)."""
    text = warnings[0] if len(warnings) == 1 else ""
    routed = text == f"{FOLLOWUP} belongs to {LEAD}, so {SECOND} cannot decide it"
    decided = text.startswith(f"{FOLLOWUP} was decided by {LEAD} at revision ")
    return routed or (decided and text.endswith(f", so {SECOND} cannot decide it"))


def _positive(world):
    raced = world.race()
    own_task = world.set_task(SECOND_NAME, "t2")
    task = next(t for t in world.doc()["tasks"] if t["id"] == "t2")
    return {
        "race": {name: result["accepted"] for name, result in sorted(raced.items())},
        "second_refusal_names_lead": names_lead(raced[SECOND_NAME]["warnings"]),
        "followup_done": world.followup()["done"],
        "deciders": {item: row["seat"] for item, row in world.decisions().items()},
        "own_task_accepted": own_task["accepted"],
        "task_description": task["description"],
        "posts": world.posts(),
        "refusals": world.refusals(),
    }


def _negative(world):
    refused = {
        "decide": world.decide(SECOND_NAME)["warnings"],
        "answer": world.answer(SECOND_NAME)["warnings"],
        "set_task": world.set_task(SECOND_NAME, "t1")["warnings"],
        "former": world.decide(FORMER)["warnings"],
    }
    doc = world.doc()
    world.operator_sets(True)
    return {
        "refused": refused,
        "followup_done": next(f["done"] for f in doc["followups"] if f["id"] == INPUTS["followup"]["id"]),
        "answers": next(q["answers"] for q in doc["questions"] if q["id"] == INPUTS["question"]["id"]),
        "t1_description": next(t.get("description", "") for t in doc["tasks"] if t["id"] == "t1"),
        "decisions": world.decisions(),
        "posts": world.posts(),
        "refusals": world.refusals(),
        "operator_write_kept": world.followup()["done"] is True,
    }


def _recovery(world):
    import fakeredis

    world.down(LEAD_NAME)
    by_acting_lead = world.decide(SECOND_NAME)
    world.seat(SUCCESSOR, LEAD)
    stale = world.decide(SUCCESSOR, value=False)
    world.operator_sets(False)
    after_reopen = world.decide(SUCCESSOR, n=1)
    restarted = fakeredis.FakeRedis(server=world.server, decode_responses=True)
    second_again = world.write(
        {"op": "set", "id": "set-again", "by": SECOND_NAME, "path": f"{FOLLOWUP}/done", "value": False},
        connect=lambda: restarted,
    )
    return {
        "acting_lead_accepted": by_acting_lead["accepted"],
        "stale_successor": [stale["accepted"], stale["warnings"]],
        "after_reopen_accepted": after_reopen["accepted"],
        "decisions": world.decisions(),
        "second_after_restart": [second_again["accepted"], second_again["warnings"]],
        "followup_done": world.followup()["done"],
        "posts": world.posts(),
        "refusals": world.refusals(),
    }


def _meets_positive(o):
    lead_won = o["race"] == {LEAD_NAME: True, SECOND_NAME: False}
    refused = o["second_refusal_names_lead"] is True
    recorded = o["deciders"] == {FOLLOWUP: LEAD}
    posted = o["posts"] == [f"{LEAD} set {FOLLOWUP}/done to true", f"{SECOND} set tasks/t2: description"]
    own = o["own_task_accepted"] and o["task_description"] == f"Updated by {SECOND_NAME}"
    return lead_won and refused and o["followup_done"] is True and recorded and posted and own and o["refusals"] == 1


def _meets_negative(o):
    expected = {
        "decide": [f"{FOLLOWUP} belongs to {LEAD}, so {SECOND} cannot decide it"],
        "answer": [f"{QUESTION} belongs to {LEAD}, so {SECOND} cannot answer it"],
        "set_task": [f"tasks/t1 belongs to {LEAD}, so {SECOND} cannot set it"],
        "former": [f"{FOLLOWUP} belongs to {LEAD}, so {FORMER} cannot decide it"],
    }
    unchanged = o["followup_done"] is False and o["answers"] == [] and o["t1_description"] == ""
    quiet = o["decisions"] == {} and o["posts"] == [] and o["refusals"] == 4
    return o["refused"] == expected and unchanged and quiet and o["operator_write_kept"]


def _meets_recovery(o):
    stale_rev = None
    stale = o["stale_successor"]
    if stale[0] is False and len(stale[1]) == 1:
        stale_rev = stale[1][0].removeprefix(f"{FOLLOWUP} was decided by {SECOND} at revision ").partition(",")[0]
    named = stale_rev is not None and stale[1][0] == (
        f"{FOLLOWUP} was decided by {SECOND} at revision {stale_rev}, so {LEAD} cannot decide it"
    )
    record = o["decisions"].get(FOLLOWUP, {})
    after = o["second_after_restart"]
    kept = after[0] is False and after[1] == [
        f"{FOLLOWUP} was decided by {LEAD} at revision {record.get('rev')}, so {SECOND} cannot decide it"
    ]
    posted = o["posts"] == [f"{SECOND} set {FOLLOWUP}/done to true", f"{LEAD} set {FOLLOWUP}/done to true"]
    decided = o["acting_lead_accepted"] and o["after_reopen_accepted"] and record.get("seat") == LEAD
    return named and decided and kept and posted and o["followup_done"] is True and o["refusals"] == 2


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
