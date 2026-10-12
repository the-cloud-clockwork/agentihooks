import pytest

from scripts.swarm_ledger import ledger_fold


def task(task_id, **fields):
    return {
        "id": task_id,
        "title": task_id,
        "lane": "eng",
        "state": "open",
        "claimed_by": "",
        "kind": "code",
        "difficulty": "M",
        "phase": "p1",
        **fields,
    }


def ledger(*tasks):
    return {
        "overview": "Make the swarm fast. Every wait costs money.",
        "phases": [{"id": "p1", "title": "Speed", "description": "Cut every wait. Later work follows."}],
        "tasks": list(tasks),
    }


def plan(lead="t1", members=("t2",), seams=("Cut the merge wait", "Drop the second poll"), done=""):
    return {"groups": [{"lead": lead, "members": list(members), "seams": list(seams), "done": done}]}


def test_the_pool_leaves_out_claimed_grouped_non_code_and_planner_chain_tasks():
    doc = ledger(
        task("t1"),
        task("t2", state="claimed", claimed_by="engineer@abcdef-0002"),
        task("t3", kind="ops"),
        task("t4", group_members=["t9"]),
        task("t5", plan_slice="first"),
        task("t6", plan_slice="second", depends_on=["t5"]),
        task("t7", state="done"),
        task("t8", merged_into="t4"),
        task("t9", plan_slice="alone"),
    )
    kept, skipped = ledger_fold.pool(doc)
    assert [t["id"] for t in kept] == ["t1", "t9"]
    assert skipped == {"frozen": 0, "in flight": 1, "planner chain": 2, "grouped": 1, "not code": 1}


def test_candidates_group_tasks_sharing_a_territory_under_the_fold_ceiling():
    doc = ledger(
        task("t1", territory=["scripts/swarm"]),
        task("t2", territory=["scripts/swarm"]),
        task("t3", territory=["scripts/swarm"], difficulty="L"),
        task("t4", territory=["docs"]),
    )
    found = ledger_fold.candidates(doc)
    assert found["pool"] == 4
    assert [[t["id"] for t in g["tasks"]] for g in found["groups"]] == [["t1", "t2"]]
    assert found["groups"][0]["signals"] == [{"between": ["t1", "t2"], "signal": "territory scripts/swarm"}]


def test_candidates_link_tasks_by_shared_code_names_and_title_words():
    doc = ledger(
        task("t1", title="Merge wait reports the pull request outcome", description="Change `merge_wait` output."),
        task("t2", title="Merge wait reports the pull request checks", description="Touches merge_wait."),
    )
    signals = [s["signal"] for s in ledger_fold.candidates(doc)["groups"][0]["signals"]]
    assert signals == ["names merge_wait", "titles share merge, pull, reports, request, wait"]


def test_a_clean_plan_has_no_problems():
    assert ledger_fold.problems(ledger(task("t1"), task("t2")), plan()) == []


def test_the_plan_check_names_every_problem():
    doc = ledger(task("t1"), task("t2"), task("t3", difficulty="L"), task("t4", difficulty="L"))
    found = ledger_fold.problems(
        doc,
        {
            "groups": [
                {"lead": "t1", "members": ["t2"], "seams": ["Fix t1 in scripts/fold.py", "One -- two"], "done": ""},
                {"lead": "t2", "members": ["t3", "t4"], "seams": ["a", "b", "c"]},
                {"lead": "t1", "members": ["missing"], "seams": ["a", "b"]},
            ]
        },
    )
    assert found == [
        "group 1: seam One names task id t1; describe the change in plain words",
        "group 1: seam One names the path scripts/fold.py; name the command, hook or behaviour instead",
        "group 1: seam Two uses the dash '--'; use a comma or a full stop",
        "group 2: task t2 sits in another group of this plan",
        "group 2: together they pass 80 minutes of work",
        "group 3: task missing is not on the ledger",
    ]


def test_the_plan_check_counts_one_seam_per_folded_task():
    found = ledger_fold.problems(ledger(task("t1"), task("t2")), plan(seams=["Only one"]))
    assert found == ["group 1: seams must list 2 plain sentences, one per folded task"]


def test_an_empty_plan_is_refused():
    assert ledger_fold.problems(ledger(task("t1")), {"groups": []})[0].startswith("the plan needs")


def test_writes_compose_the_lead_description_territory_and_member_status():
    doc = ledger(
        task("t1", title="Merge wait reports outcomes", territory=["scripts/swarm", "tests/swarm"]),
        task("t2", territory=["scripts/swarm", "docs"]),
    )
    planned = ledger_fold.writes(doc, plan()["groups"][0])
    assert planned["group"] == {"item": "tasks/t1", "members": ["t2"]}
    assert planned["lead"] == {
        "description": "\n".join(
            [
                "Parent intent: Make the swarm fast.",
                "Mission: Speed: Cut every wait.",
                "This task delivers 2 folded changes in one pull request.",
                "One: Cut the merge wait.",
                "Two: Drop the second poll.",
                "Done when one merged pull request delivers every numbered change above.",
            ]
        ),
        "territory": ["scripts/swarm", "tests/swarm", "docs"],
    }
    assert planned["statuses"] == {
        "t2": "Folded into the task titled Merge wait reports outcomes; "
        "its agent delivers this change in the same pull request."
    }


def test_a_lead_title_that_is_not_plain_falls_back_to_plain_status():
    doc = ledger(task("t1", title="Fix scripts/swarm/merge.py"), task("t2"))
    assert ledger_fold.writes(doc, plan()["groups"][0])["statuses"]["t2"] == ledger_fold.FALLBACK_STATUS


def test_the_pool_reads_time_left_inputs_and_leaves_out_what_the_freeze_holds(monkeypatch):
    seen = {}

    def frozen(doc, pending, inputs):
        seen["inputs"] = inputs
        return {"t2"}

    monkeypatch.setattr(ledger_fold.ledger_stats, "frozen", frozen)
    doc = ledger(task("t1"), task("t2"), task("t3", claimed_by="engineer@abcdef-0002"))
    doc["_meta"] = {"time_left": {"inputs": {"slots": 3}}}
    kept, skipped = ledger_fold.pool(doc)
    assert seen["inputs"] == {"slots": 3}
    assert [t["id"] for t in kept] == ["t1"]
    assert skipped["frozen"] == 1 and skipped["in flight"] == 1


def test_planner_chains_count_every_linked_slice_by_either_field():
    doc = ledger(
        task("t1", slice="a"),
        task("t2", slice="b", depends_on=["t1"]),
        task("t3", plan_slice="c"),
        task("t4", plan_slice="d", depends_on=["t3"]),
    )
    assert ledger_fold.pool(doc)[1]["planner chain"] == 4


def test_names_come_from_the_title_and_the_description_alone():
    assert ledger_fold.names({"title": "Fix merge_wait"}) == {"merge_wait"}
    assert ledger_fold.names({"description": "Touches claim_order."}) == {"claim_order"}
    assert ledger_fold.names({"title": None, "description": None}) == set()


def test_signals_join_every_shared_item_and_need_a_real_overlap():
    a = task("t1", territory=["docs", "scripts"], description="`merge_wait` and `claim_order`")
    b = task("t2", territory=["docs", "scripts"], description="`claim_order` then `merge_wait`")
    assert ledger_fold.signals(a, b) == ["territory docs, scripts", "names claim_order, merge_wait"]
    assert ledger_fold.signals(task("t1", description="`merge_wait`"), task("t2", description="`claim_order`")) == []
    assert ledger_fold.signals({"id": "t1"}, {"id": "t2"}) == []


def test_title_words_link_at_exactly_three_fifths_and_ignore_short_words():
    sharing = task("t1", title="alpha bravo charlie delta echo")
    assert ledger_fold.signals(sharing, task("t2", title="alpha bravo charlie golf hotel")) == [
        "titles share alpha, bravo, charlie"
    ]
    assert ledger_fold.signals(task("t1", title="alpha bravo"), task("t2", title="alpha golf")) == []
    assert ledger_fold.signals(task("t1", title="alpha cat"), task("t2", title="t2")) == []
    assert ledger_fold.words("cat dog alpha") == {"alpha"}


def test_candidates_fill_a_group_to_five_and_start_the_next_one():
    doc = ledger(*(task(f"t{n}", difficulty="S", territory=["scripts"]) for n in range(1, 8)))
    found = ledger_fold.candidates(doc)
    assert [[t["id"] for t in g["tasks"]] for g in found["groups"]] == [["t1", "t2", "t3", "t4", "t5"], ["t6", "t7"]]
    assert len(found["groups"][0]["signals"]) == 10
    assert set(found) == {"pool", "skipped", "groups"} and found["groups"][0]["tasks"][0]["kind"] == "code"


def test_the_plan_check_refuses_malformed_groups_with_their_reason():
    doc = ledger(task("t1"), task("t2"), task("t3", depends_on=["t9"]), task("t9", state="open"))
    cases = [
        ("not a group", "each group is an object with a lead, members, seams and done"),
        ({"lead": 1, "members": ["t2"]}, "needs a lead id and at least one member id"),
        ({"lead": "t1", "members": []}, "needs a lead id and at least one member id"),
        ({"lead": "t1", "members": [2]}, "members must be task ids"),
    ]
    for group, reason in cases:
        assert ledger_fold.problems(doc, {"groups": [group]}) == [f"group 1: {reason}"]
    found = ledger_fold.problems(
        doc, {"groups": [{"lead": "t1", "members": ["t3"], "seams": ["One", ""], "done": "see a/b"}]}
    )
    assert found == [
        "group 1: task t3 waits on task t9",
        "group 1: seams must list 2 plain sentences, one per folded task",
        "group 1: done names the path a/b; name the command, hook or behaviour instead",
    ]
    assert ledger_fold.problems(doc, {"groups": [{**plan()["groups"][0], "done": 3}]}) == ["group 1: done must be text"]


def test_the_description_trims_full_stops_and_states_what_the_ledger_lacks():
    group = {"lead": "t1", "members": ["t2"], "seams": ["Cut the wait.", ".Drop the poll"], "done": "it merges."}
    lines = ledger_fold.description({"tasks": []}, task("t1", phase="none"), group).splitlines()
    assert lines[:2] == ["Parent intent: not stated on the ledger.", "Mission: not stated on the ledger."]
    assert lines[3:] == ["One: Cut the wait.", "Two: .Drop the poll.", "Done when it merges."]
    assert ledger_fold.description({"overview": "", "phases": []}, task("t1"), group).startswith(
        "Parent intent: not stated on the ledger."
    )


def test_the_member_status_names_the_lead_title_without_its_full_stop():
    known = {"t1": task("t1")}
    assert ledger_fold.status({"title": "Tidy the UX."}, known) == (
        "Folded into the task titled Tidy the UX; its agent delivers this change in the same pull request."
    )
    assert ledger_fold.status({}, known) == ledger_fold.FALLBACK_STATUS


def test_candidates_leave_out_a_task_still_waiting_on_an_open_dependency():
    doc = ledger(task("t1", territory=["scripts"]), task("t2", territory=["scripts"], depends_on=["t9"]), task("t9"))
    assert ledger_fold.candidates(doc)["groups"] == []


def test_a_missing_title_never_matches_a_real_title_word():
    assert ledger_fold.signals({"id": "t1"}, task("t2", title="xxxx")) == []
    assert ledger_fold.signals(task("t1", title="xxxx"), {"id": "t2"}) == []


def test_full_stops_are_trimmed_and_capital_letters_kept():
    group = {"lead": "t1", "members": ["t2"], "seams": ["Tidy the UX.", "Fix the X"], "done": "the panel shows UX."}
    lines = ledger_fold.description({"overview": "Ship the UX. Then more."}, task("t1"), group).splitlines()
    assert lines[0] == "Parent intent: Ship the UX."
    assert lines[3:] == ["One: Tidy the UX.", "Two: Fix the X.", "Done when the panel shows UX."]


def test_a_name_opening_the_description_stands_apart_from_the_title():
    assert ledger_fold.names({"title": "Fix", "description": "merge_wait handles it"}) == {"merge_wait"}


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("see (a/b).", "a/b"),
        ("read notes.md.", "notes.md"),
        ("/etc/passwd here", "/etc/passwd"),
        ("use / here", ""),
        ("move X/Y now", "X/Y"),
        ("see X/ now", "X/"),
        ("the yaml route", ""),
    ],
)
def test_a_path_is_found_word_by_word_without_its_wrapping(text, path):
    assert ledger_fold._path(text) == path


def test_a_long_text_without_a_path_is_checked_in_linear_time():
    assert ledger_fold._path("a" * 200_000) == ""
