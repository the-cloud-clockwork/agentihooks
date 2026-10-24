import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.swarm_ledger.repository  # noqa: F401  puts the ledger folder on sys.path before ledger_core loads
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_v2 import master_fence, masters
from scripts.swarm_v2.masters_channel import MastersChannel, Post
from tests import sv2_mst04_cases as cases
from tests.sv2_mst04_cases import FOLLOWUP, LEAD, LEAD_NAME, QUESTION, SECOND, SECOND_NAME, SLUG

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MST-04"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path / "ledgers")
    core.LEDGER_DIR.mkdir()
    return cases.World()


def unreachable():
    raise AssertionError("the fence connected to Redis for a write it does not cover")


@pytest.mark.parametrize(
    ("op", "item"),
    [
        ({"op": "answer", "item": "questions/q1"}, "questions/q1"),
        ({"op": "task_update", "item": "tasks/t1"}, "tasks/t1"),
        ({"op": "set", "path": "followups/f1/done"}, "followups/f1"),
        ({"op": "set", "path": "followups/f1/needs_operator"}, "followups/f1"),
        ({"op": "set", "path": "questions/q1/out_of_scope"}, "questions/q1"),
        ({"op": "set", "path": "phases/p1/done"}, ""),
        ({"op": "set", "path": "tasks/t1/out_of_scope"}, ""),
        ({"op": "set", "path": "time_left_minutes"}, ""),
        ({"op": "add", "thread": "followups/f1/comments"}, ""),
        ({"op": "retext", "item": "followups/f1"}, ""),
        ({"op": "task_add", "item": "tasks/t1"}, ""),
    ],
)
def test_only_answers_decisions_and_task_updates_are_covered(op, item):
    assert master_fence.decision(op) == item


def test_each_covered_write_has_its_verb():
    assert [master_fence.verb({"op": kind}) for kind in ("answer", "set", "task_update")] == ["answer", "decide", "set"]


def test_the_refusals_name_the_owning_master_in_full():
    assert master_fence.refusal("followups/f1", "master@sw", "master-2@sw", "decide") == (
        "followups/f1 belongs to master@sw, so master-2@sw cannot decide it"
    )
    assert master_fence.refusal("questions/q1", "master-2@sw", "master@sw", "answer", 7) == (
        "questions/q1 was decided by master-2@sw at revision 7, so master@sw cannot answer it"
    )


OWN = {"followups/f1/done": {"rev": 5, "by": "master@abcdef-0001", "at": 10}}


@pytest.mark.parametrize(
    ("stamps", "held"),
    [
        (OWN, True),
        ({**OWN, "followups/f1/needs_operator": {"rev": 9, "by": "master@abcdef-0002"}}, True),
        ({**OWN, "followups/f1/comments": {"rev": 9, "by": "engineer@abcdef-0005"}}, True),
        ({**OWN, "followups/f1/text": {"rev": 4, "by": "operator"}}, True),
        ({**OWN, "followups/f1/text": {"rev": 5, "by": "operator", "at": 10}}, True),
        ({**OWN, "followups/f12/done": {"rev": 9, "by": "operator"}}, True),
        ({**OWN, "followups/f1/needs_operator": {"rev": 6, "by": "operator"}}, False),
        ({**OWN, "followups/f1/text": {"rev": 6, "by": "engineer@abcdef-0005"}}, False),
        ({"followups/f1/done": {"rev": 6, "by": "operator"}}, False),
        ({"followups/f1/done": {"rev": 4, "by": "master@abcdef-0001"}}, False),
        ({}, False),
    ],
)
def test_a_decider_record_holds_while_its_write_stands_and_only_masters_wrote_since(stamps, held):
    assert master_fence.current({"seat": "master@sw", "rev": 5}, "followups/f1", stamps, 0) is held


@pytest.mark.parametrize(("retired_at", "held"), [(9, True), (10, False), (11, False)])
def test_a_decider_record_stamped_before_its_seat_last_retired_no_longer_holds(retired_at, held):
    assert master_fence.current({"seat": "master@sw", "rev": 5}, "followups/f1", OWN, retired_at) is held


@pytest.mark.parametrize(
    ("op", "text"),
    [
        (
            {"op": "answer", "item": "questions/q1", "text": "Use the spare port"},
            "master@sw answered questions/q1: Use the spare port",
        ),
        ({"op": "set", "path": "followups/f1/done", "value": True}, "master@sw set followups/f1/done to true"),
        ({"op": "set", "path": "followups/f1/done", "value": False}, "master@sw set followups/f1/done to false"),
        (
            {"op": "task_update", "item": "tasks/t1", "fields": {"state": "blocked", "description": "d"}},
            "master@sw set tasks/t1: description, state",
        ),
    ],
)
def test_the_masters_channel_line_says_what_was_decided(op, text):
    assert master_fence.announcement("master@sw", op) == text


def test_a_decision_is_posted_as_the_master_seat_that_made_it(world):
    post = MastersChannel(world.redis).decision(SLUG, SECOND, SECOND_NAME, "decided", 12.5)

    assert post == Post(1, SECOND, SECOND_NAME, "decided", 12.5)
    assert MastersChannel(world.redis).read(SLUG, LEAD_NAME) == [post]


@pytest.mark.parametrize(
    ("op", "text"),
    [
        (
            {"op": "set", "path": "followups/f1/done", "value": True},
            "master-2@sw set followups/f1/done to true for master@sw",
        ),
        (
            {"op": "answer", "item": "questions/q1", "text": " Use the spare port "},
            "master-2@sw answered questions/q1 for master@sw: Use the spare port",
        ),
        (
            {"op": "task_update", "item": "tasks/t1", "fields": {"state": "blocked"}},
            "master-2@sw set tasks/t1: state for master@sw",
        ),
    ],
)
def test_the_masters_channel_line_names_the_owner_of_a_cross_write(op, text):
    assert master_fence.announcement("master-2@sw", op, "master@sw") == text
    assert master_fence.announcement("master@sw", op, "master@sw") == master_fence.announcement("master@sw", op)


def cross_rows(world):
    return {item: [(row["owner"], row["seat"], row["by"]) for row in rows] for item, rows in world.cross().items()}


def test_two_masters_racing_on_one_follow_up_are_both_accepted_and_the_owner_kept(world):
    raced = world.race()

    assert raced == {name: {"accepted": True, "warnings": []} for name in (LEAD_NAME, SECOND_NAME)}
    assert world.followup()["done"] is True
    assert world.decisions()[FOLLOWUP]["seat"] == LEAD
    assert world.refusals() == 0


def test_a_live_master_decides_a_follow_up_another_master_owns_and_the_write_is_recorded(world):
    assert world.decide(SECOND_NAME) == {"accepted": True, "warnings": []}

    doc = world.doc()
    rev = doc["_meta"]["stamps"][f"{FOLLOWUP}/done"]["rev"]
    assert doc["_meta"][master_fence.DECISIONS] == {FOLLOWUP: {"seat": LEAD, "rev": rev}}
    assert doc["_meta"][master_fence.CROSS] == {
        FOLLOWUP: [{"owner": LEAD, "seat": SECOND, "by": SECOND_NAME, "rev": rev}]
    }
    assert world.posts() == [f"{SECOND} set {FOLLOWUP}/done to true for {LEAD}"]
    assert world.refusals() == 0


def test_a_later_cross_write_keeps_the_decider_record_on_the_owner(world):
    world.decide(LEAD_NAME)

    assert world.decide(SECOND_NAME, value=False) == {"accepted": True, "warnings": []}
    assert world.followup()["done"] is False
    assert world.decisions()[FOLLOWUP]["seat"] == LEAD
    assert cross_rows(world) == {FOLLOWUP: [(LEAD, SECOND, SECOND_NAME)]}


def test_every_cross_write_on_an_item_is_kept_up_to_the_cap(world):
    for n in range(master_fence.CROSS_KEPT + 1):
        world.decide(SECOND_NAME, value=n % 2 == 0, n=n)

    rows = world.cross()[FOLLOWUP]
    assert len(rows) == master_fence.CROSS_KEPT
    assert [row["rev"] for row in rows] == sorted(row["rev"] for row in rows)
    assert rows[-1]["rev"] == world.doc()["_meta"]["stamps"][f"{FOLLOWUP}/done"]["rev"]


def test_a_master_without_a_live_seat_is_still_refused_naming_the_owner(world):
    world.down(SECOND_NAME)

    assert world.decide(SECOND_NAME) == {
        "accepted": False,
        "warnings": [f"{FOLLOWUP} belongs to {LEAD}, so {SECOND_NAME} cannot decide it"],
    }
    assert world.followup()["done"] is False
    assert world.refusals() == 1


def test_an_accepted_decision_records_its_seat_and_revision(world):
    world.decide(LEAD_NAME)

    doc = world.doc()
    assert doc["_meta"][master_fence.DECISIONS] == {
        FOLLOWUP: {"seat": LEAD, "rev": doc["_meta"]["stamps"][f"{FOLLOWUP}/done"]["rev"]}
    }


def test_a_master_sets_a_task_in_any_phase_and_a_cross_write_names_its_owner(world):
    assert world.set_task(SECOND_NAME, "t2") == {"accepted": True, "warnings": []}
    assert world.set_task(SECOND_NAME, "t1") == {"accepted": True, "warnings": []}
    assert world.set_task(LEAD_NAME, "t1")["accepted"] is True
    assert world.posts() == [
        f"{SECOND} set tasks/t2: description",
        f"{SECOND} set tasks/t1: description for {LEAD}",
        f"{LEAD} set tasks/t1: description",
    ]
    assert world.decisions() == {}
    assert cross_rows(world) == {"tasks/t1": [(LEAD, SECOND, SECOND_NAME)]}


def test_a_task_with_no_phase_belongs_to_the_lead(world):
    core.sync(
        SLUG, ops=[{"op": "task_add", "id": "add-t3", "by": "swarm", "task": "t3", "title": "Loose", "lane": "eng"}]
    )

    assert world.set_task(SECOND_NAME, "t3")["accepted"] is True
    assert cross_rows(world) == {"tasks/t3": [(LEAD, SECOND, SECOND_NAME)]}


def test_a_refused_write_changes_nothing_and_posts_nothing(world):
    before = world.doc()

    assert world.answer(cases.FORMER) == {
        "accepted": False,
        "warnings": [f"{QUESTION} belongs to {LEAD}, so {cases.FORMER} cannot answer it"],
    }
    after = world.doc()
    assert after["questions"] == before["questions"]
    assert after["_meta"]["stamps"] == before["_meta"]["stamps"]
    assert world.posts() == []
    assert world.cross() == {}
    assert world.refusals() == 1


def test_a_decision_that_changes_nothing_records_and_posts_nothing(world):
    assert world.decide(LEAD_NAME, value=False)["accepted"] is True

    assert world.decisions() == {}
    assert world.posts() == []


def test_the_owner_may_decide_its_follow_up_again(world):
    world.decide(LEAD_NAME)

    assert world.decide(LEAD_NAME, value=False, n=1) == {"accepted": True, "warnings": []}
    assert world.followup()["done"] is False
    assert world.posts() == [f"{LEAD} set {FOLLOWUP}/done to true", f"{LEAD} set {FOLLOWUP}/done to false"]


def test_a_decider_whose_seat_is_gone_no_longer_fences_the_item(world):
    world.down(LEAD_NAME)
    world.decide(SECOND_NAME)
    world.store.drop_agent(SLUG, SECOND_NAME)
    masters.MasterSeats(world.redis).set_count(SLUG, 1)
    world.store.put_agent(SLUG, AgentRecord(LEAD_NAME, MASTER, MASTER, pane_id="pm", seat=LEAD))

    assert world.decide(LEAD_NAME, value=False) == {"accepted": True, "warnings": []}
    assert world.decisions()[FOLLOWUP]["seat"] == LEAD


def test_a_successor_decides_over_a_record_whose_seat_remains_and_the_record_keeps_that_seat(world):
    world.down(LEAD_NAME)
    world.decide(SECOND_NAME)
    world.down(SECOND_NAME)
    world.seat(cases.SUCCESSOR, LEAD)

    assert world.decide(cases.SUCCESSOR, value=False) == {"accepted": True, "warnings": []}
    assert world.decisions()[FOLLOWUP]["seat"] == SECOND
    assert cross_rows(world) == {FOLLOWUP: [(SECOND, LEAD, cases.SUCCESSOR)]}


def test_repeating_a_decision_posts_it_once(world):
    world.decide(LEAD_NAME)

    assert world.decide(LEAD_NAME, n=1) == {"accepted": True, "warnings": []}
    assert world.posts() == [f"{LEAD} set {FOLLOWUP}/done to true"]


def test_a_posted_decision_carries_its_seat_author_and_write_time(world):
    world.decide(LEAD_NAME)
    at = world.doc()["_meta"]["stamps"][f"{FOLLOWUP}/done"]["at"]

    assert MastersChannel(world.redis).read(SLUG, LEAD_NAME) == [
        Post(1, LEAD, LEAD_NAME, f"{LEAD} set {FOLLOWUP}/done to true", at / 1000)
    ]


def test_a_single_master_swarm_decides_as_before_and_posts_nothing(world):
    world.store.drop_agent(SLUG, SECOND_NAME)
    masters.MasterSeats(world.redis).set_count(SLUG, 1)

    assert world.decide(LEAD_NAME) == {"accepted": True, "warnings": []}
    assert world.answer(LEAD_NAME)["accepted"] is True
    assert world.posts() == []
    assert world.decisions()[FOLLOWUP]["seat"] == LEAD


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add", "thread": f"{FOLLOWUP}/comments", "id": "c1", "by": SECOND_NAME, "text": "Looking at it now"},
        {"op": "set", "id": "s1", "by": SECOND_NAME, "path": "phases/p1/done", "value": True},
        {"op": "set", "id": "s2", "by": cases.ENGINEER, "path": f"{FOLLOWUP}/done", "value": True},
        {"op": "add", "thread": "notes", "id": "n1", "text": "An operator note"},
    ],
)
def test_writes_it_does_not_cover_pass_without_reading_redis(world, op):
    assert world.write(op, connect=unreachable)["accepted"] is True


def test_a_covered_op_with_no_author_passes_without_reading_redis():
    fence = master_fence.Fence(SLUG, connect=unreachable)

    assert fence.apply({}, {"op": "answer", "item": QUESTION}, object(), lambda doc, op, ctx: "applied") == "applied"


def test_the_author_is_resolved_once_through_the_write_context(world, monkeypatch):
    calls = []

    def resolve(name):
        calls.append(name)
        return name

    monkeypatch.setattr("scripts.swarm.naming.resolve_name", resolve)
    world.decide(LEAD_NAME)

    assert calls == [LEAD_NAME]


def test_a_ledger_with_no_swarm_is_not_fenced(world):
    world.store.redis.delete(world.store.key(SLUG, "config"))

    assert world.decide(SECOND_NAME) == {"accepted": True, "warnings": []}
    assert world.decisions() == {}


@pytest.mark.parametrize(("switch", "accepted"), [("off", True), ("on", False), ("", False)])
def test_the_fence_is_rolled_back_by_turning_its_switch_off(world, monkeypatch, switch, accepted):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_FENCE", switch)

    assert (
        world.write(
            {"op": "set", "id": "s-switch", "by": cases.FORMER, "path": f"{FOLLOWUP}/done", "value": True},
            connect=unreachable if accepted else None,
        )["accepted"]
        is accepted
    )
    assert world.refusals() == int(not accepted)


def test_redis_down_lets_the_write_through_with_a_fail_open_row(world, tmp_path):
    def down():
        raise ConnectionError("redis down")

    fence = master_fence.Fence(SLUG, connect=down, home=tmp_path)
    op = {"op": "set", "id": "s-down", "by": SECOND_NAME, "path": f"{FOLLOWUP}/done", "value": True}
    _, rejected = core.sync(SLUG, ops=[op], gate=fence)

    assert rejected == []
    rows = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "log.jsonl").read_text().splitlines()]
    assert [(r["gate"], r["kind"], r["agent"], r["tool"], r["reason"]) for r in rows] == [
        ("master-fence", "fail-open", SECOND_NAME, "ledger", "ConnectionError: redis down")
    ]


def test_the_fence_wraps_the_inner_gate(world):
    seen = []

    class Inner:
        def apply(self, doc, op, ctx, apply):
            seen.append(op["id"])
            return apply(doc, op, ctx)

    fence = master_fence.Fence(SLUG, Inner(), connect=lambda: world.redis)
    ops = [
        {"op": "set", "id": "mine", "by": LEAD_NAME, "path": f"{FOLLOWUP}/done", "value": True},
        {"op": "set", "id": "theirs", "by": cases.FORMER, "path": f"{FOLLOWUP}/done", "value": False},
    ]
    _, rejected = core.sync(SLUG, ops=ops, gate=fence)

    assert (seen, rejected) == (["mine"], ["theirs"])
    assert fence.inner is not None


def test_publish_skips_a_write_the_ledger_rejected_after_the_fence(world):
    fence = master_fence.Fence(SLUG, connect=lambda: world.redis)
    op = {"op": "set", "id": "s-rolled", "by": LEAD_NAME, "path": f"{FOLLOWUP}/done", "value": True}
    core.sync(SLUG, ops=[op], gate=fence)

    fence.publish(["s-rolled"])
    assert world.posts() == []
    fence.publish([])
    assert world.posts() == [f"{LEAD} set {FOLLOWUP}/done to true"]


def test_refusals_are_counted_per_swarm(world):
    world.decide(cases.FORMER)
    world.answer(cases.FORMER)

    assert master_fence.measurements(world.redis, SLUG) == {"master_write_refusals_total": 2}
    assert master_fence.measurements(world.redis, "other") == {"master_write_refusals_total": 0}


def test_the_server_fences_master_writes_inside_the_talk_budget():
    from scripts.gates import talk
    from scripts.swarm_ledger import ledger_server

    gate = ledger_server.write_gate(SLUG)

    budget = gate.inner.inner
    assert (type(gate), gate.slug, type(budget), budget.slug) == (master_fence.Fence, SLUG, talk.Budget, SLUG)


def test_the_api_publishes_decisions_after_the_write(monkeypatch):
    from scripts.swarm_ledger import ledger_server
    from scripts.swarm_ledger.api import mutations

    published, gates = [], []

    class Gate:
        def apply(self, doc, op, ctx, apply):
            return True

        def publish(self, rejected):
            published.append(rejected)

    def apply_ops(slug, changes=None, ops=None, gate=None, *, copy_state=True):
        gates.append(gate.inner.budget)
        return {"_meta": {"rev": 2, "warnings": []}, "tasks": []}, ["b"]

    monkeypatch.setattr(ledger_server, "write_gate", lambda slug: Gate())
    monkeypatch.setattr(
        ledger_server.repository, "current", lambda slug: SimpleNamespace(state={"tasks": [], "_meta": {}})
    )
    monkeypatch.setattr(ledger_server.repository, "apply_ops", apply_ops)
    for name in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(ledger_server, name, lambda slug, state: None)
    ops = [{"op": "ack", "id": n, "by": LEAD_NAME, "rev": 1} for n in ("a", "b")]
    reply = mutations.apply(ledger_server.api_services(), SLUG, "", {"operation_id": "o1", "ops": ops, "guards": {}})

    assert [type(g).__name__ for g in gates] == ["Gate"]
    assert published == [["b"]]
    assert reply["rejected"] == ["b"]


@pytest.mark.parametrize(("ops", "gated"), [([{"op": "ack", "id": "ack-1"}], True), (None, False)])
def test_the_page_write_publishes_decisions_after_the_write(monkeypatch, ops, gated):
    from scripts.swarm_ledger import ledger_server

    published, seen = [], []

    class Gate:
        def publish(self, rejected):
            published.append(rejected)

    def apply_ops(slug, changes=None, ops=None, gate=None):
        seen.append(type(gate).__name__)
        return {"_meta": {"members": {}, "rev": 1}, "tasks": []}, ["ack-1"]

    monkeypatch.setattr(ledger_server, "write_gate", lambda slug: Gate())
    monkeypatch.setattr(ledger_server.repository, "apply_ops", apply_ops)
    monkeypatch.setattr(ledger_server, "relay_to_inbox", lambda slug, state: [])
    monkeypatch.setattr(ledger_server, "doctor_phrase", lambda slug, state: None)
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.path = f"/api/{SLUG}?view=agent"
    handler.send = lambda code, body, ctype: code
    assert handler.reply_state(SLUG, ops=ops) == 200
    assert (seen, published) == ((["Gate"], [["ack-1"]]) if gated else (["NoneType"], []))


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text(encoding="utf-8"))
    assert committed == {"case": f"T-SV2-MST-04-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )


def test_the_manifest_names_the_hash_of_every_case_input():
    import hashlib

    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]
    assert manifest["inputs"] == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in ["tests/fixtures/swarm_v2/master-race.json"]
    }
    assert manifest["cases"] == ["T-SV2-MST-04-A", "T-SV2-MST-04-B", "T-SV2-MST-04-C"]


def test_the_package_record_carries_its_completion_evidence():
    record = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    assert (record["package"], record["package_complete"], record["integration_gate"]) == ("SV2-MST-04", False, "G10")
    assert set(record["states"]) == {"observed", "committed", "externally_verified"}
    for name in ("interfaces", "supported_versions", "authoritative_objects", "remaining_limitations", "validation"):
        assert record[name], name
    assert set(record["measurements"]) == {"master_write_refusals_total"}
    assert record["cases"] == {
        **{case: f"evidence/SV2-MST-04/{case}-result.json" for case in "abc"},
        "manifest": "evidence/SV2-MST-04/manifest.json",
    }
    source = Path(__file__).read_text(encoding="utf-8")
    path, name = record["rollback_rehearsal"]["test"].split("::")
    assert path == "tests/test_swarm_v2_master_fence.py"
    assert f"\ndef {name}(" in source
    for obligation in record["obligations"].values():
        assert obligation["met_by"]
        for test in obligation["tests"]:
            path, function = test.split("::")
            assert path == "tests/test_swarm_v2_master_fence.py"
            assert f"\ndef {function}(" in source, test
