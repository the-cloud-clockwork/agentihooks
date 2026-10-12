from scripts.swarm_ledger import ledger_fold


def task(task_id, **fields):
    return {
        "id": task_id,
        "title": f"Change number {task_id[1:]}",
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
