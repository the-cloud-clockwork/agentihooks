import sys
import unittest
import unittest.mock
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "tasks-2026-01-01"


def make_ledger(tasks=()):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "tasks": list(tasks),
    }
    html_path, json_path = legacy_page.paths(SLUG, core)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def op(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "swarm", **fields}


class Tasks(unittest.TestCase):
    def test_a_new_ledger_carries_its_tasks_open_and_linked_to_a_phase(self):
        state = make_ledger([{"title": "write the docs", "description": "d", "phase": "p1", "lane": "eng"}])
        task = state["tasks"][0]
        self.assertEqual(
            (task["id"], task["phase"], task["lane"], task["state"], task["done"]), ("t1", "p1", "eng", "open", False)
        )

    def test_task_add_appends_an_open_task_in_its_lane(self):
        make_ledger()
        state, rejected = core.sync(
            SLUG, ops=[op("task_add", 1, task="t7", title="speed up the tests", phase="p1", lane="ci")]
        )
        self.assertEqual(rejected, [])
        self.assertEqual([(t["id"], t["lane"], t["state"]) for t in state["tasks"]], [("t7", "ci", "open")])

    def test_task_update_records_claim_issue_pr_and_done(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        fields = {"state": "claimed", "claimed_by": "smoke-eng-1", "issue_url": "https://github.com/o/r/issues/1"}
        core.sync(SLUG, ops=[op("task_update", 1, item="tasks/t1", fields=fields)])
        state, rejected = core.sync(
            SLUG,
            ops=[
                op(
                    "task_update",
                    2,
                    item="tasks/t1",
                    fields={"state": "done", "pr_url": "https://github.com/o/r/pull/2"},
                )
            ],
        )
        task = state["tasks"][0]
        self.assertEqual(rejected, [])
        self.assertEqual(
            (task["state"], task["claimed_by"], task["pr_url"], task["done"]),
            ("done", "smoke-eng-1", "https://github.com/o/r/pull/2", True),
        )

    def test_review_history_records_pull_request_identity_and_parking(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}, {"title": "dependency", "phase": "p1"}])
        first, second = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
        changes = [
            {"state": "pr", "pr_url": first, "branch": "first"},
            {"pr_url": second, "branch": "second"},
            {"parked_on": ["t2"], "stacked_base": "origin/first"},
            {"parked_on": []},
        ]
        for index, fields in enumerate(changes):
            state, rejected = core.sync(SLUG, ops=[op("task_update", index, item="tasks/t1", fields=fields)])
            self.assertEqual(rejected, [])
        found = [
            event for event in state["_meta"]["events"] if event["kind"] in ("task pr", "task parked", "task unparked")
        ]
        self.assertEqual([event["target"] for event in found], ["tasks/t1"] * 4)
        self.assertEqual([event["by"] for event in found], ["swarm"] * 4)
        self.assertEqual(
            [(event["kind"], event.get("pr_url"), event.get("branch")) for event in found],
            [
                ("task pr", first, "first"),
                ("task pr", second, "second"),
                ("task parked", None, None),
                ("task unparked", None, None),
            ],
        )

    def test_review_history_keeps_missing_legacy_branch_empty_when_the_pull_request_changes(self):
        state = make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        task = state["tasks"][0]
        task.update(state="pr", pr_url="https://github.com/o/r/pull/1")
        task.pop("branch", None)
        context = core.Context(state["_meta"], 1000)
        self.assertTrue(
            core.apply_op(
                state,
                op("task_update", 1, item="tasks/t1", fields={"pr_url": "https://github.com/o/r/pull/2"}),
                context,
            )
        )
        self.assertEqual(context.refused, [])
        self.assertEqual(
            [(event["kind"], event["target"], event["pr_url"], event["branch"]) for event in context.events],
            [("task pr", "tasks/t1", "https://github.com/o/r/pull/2", "")],
        )

    def test_review_history_keeps_unknown_legacy_pull_request_fields_empty(self):
        state = make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        task = state["tasks"][0]
        task.pop("branch", None)
        task.pop("pr_url", None)
        context = core.Context(state["_meta"], 1000)
        self.assertTrue(core.apply_op(state, op("task_update", 1, item="tasks/t1", fields={"state": "pr"}), context))
        self.assertEqual(context.refused, [])
        self.assertEqual(
            [(event["kind"], event["pr_url"], event["branch"]) for event in context.events],
            [("task pr", "", "")],
        )

    def test_task_update_rewrites_the_description(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng", "description": "old spec"}])
        update = op("task_update", 1, item="tasks/t1", fields={"description": "new spec"})
        core.check_op(update)
        state, rejected = core.sync(SLUG, ops=[update])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["description"], "new spec")

    def test_bad_lane_state_or_field_is_refused(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        for bad in (
            op("task_add", 3, task="t9", title="x", phase="p1", lane="ops"),
            op("task_update", 4, item="tasks/t1", fields={"state": "finished"}),
            op("task_update", 5, item="tasks/t1", fields={"title": "renamed"}),
        ):
            with self.assertRaises(ValueError):
                core.check_op(bad)

    def test_an_old_ledger_without_tasks_still_syncs(self):
        state = make_ledger()
        self.assertEqual(state["tasks"], [])


class OwnershipGuards(unittest.TestCase):
    def test_an_old_claim_generation_cannot_clear_a_later_claim_by_the_same_owner(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        for n, owner in enumerate(("engineer-one", "engineer-two", "engineer-one"), 1):
            state, rejected = core.sync(
                SLUG, ops=[op("task_update", n, item="tasks/t1", fields={"state": "pr", "claimed_by": owner})]
            )
            self.assertEqual(rejected, [])
            self.assertEqual(state["tasks"][0].get("claim_generation"), n)
        stale = op(
            "task_update",
            4,
            item="tasks/t1",
            fields={"state": "open", "claimed_by": ""},
            if_match={"claimed_by": "engineer-one", "claim_generation": 1},
        )
        state, rejected = core.sync(SLUG, ops=[stale])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["claimed_by"], "engineer-one")
        self.assertEqual(state["tasks"][0]["state"], "pr")
        fresh = op(
            "task_update",
            5,
            item="tasks/t1",
            fields={"state": "open", "claimed_by": ""},
            if_match={"claimed_by": "engineer-one", "claim_generation": 3},
        )
        state, rejected = core.sync(SLUG, ops=[fresh])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["claimed_by"], "")
        self.assertEqual(state["tasks"][0]["claim_generation"], 4)

    def test_a_mismatched_claim_guard_is_a_noop_at_the_ledger_write(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        stale = op(
            "task_update",
            1,
            item="tasks/t1",
            fields={"state": "claimed", "claimed_by": "new-owner"},
            if_match={"claimed_by": "old-owner", "claim_generation": 0},
        )
        state, rejected = core.sync(SLUG, ops=[stale])
        self.assertEqual(rejected, [])
        self.assertEqual(state["tasks"][0]["state"], "open")
        self.assertEqual(state["tasks"][0]["claimed_by"], "")


class OwnershipOperationContract(unittest.TestCase):
    def test_the_operation_consumer_counts_claim_generations_and_rejects_aba(self):
        from scripts.swarm_ledger import ledger_tasks

        doc = make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        ctx = core.Context(doc["_meta"], 1_000, SLUG)
        task = doc["tasks"][0]
        for n, owner in enumerate(("first", "second", "first"), 1):
            change = op("task_update", n, item="tasks/t1", fields={"state": "pr", "claimed_by": owner})
            ledger_tasks.check(change)
            self.assertTrue(ledger_tasks.apply(doc, change, ctx))
            self.assertEqual(task["claim_generation"], n)
        for n, generation in enumerate((1, 3), 4):
            change = op(
                "task_update",
                n,
                item="tasks/t1",
                fields={"state": "open", "claimed_by": ""},
                if_match={"claimed_by": "first", "claim_generation": generation},
            )
            ledger_tasks.check(change)
            self.assertTrue(ledger_tasks.apply(doc, change, ctx))
            self.assertEqual(task["state"], "pr" if generation == 1 else "open")
        self.assertEqual(task["claim_generation"], 4)
        self.assertEqual(ctx.refused, [])

    def test_the_operation_guard_matches_every_ownership_field_and_legacy_defaults(self):
        from scripts.swarm_ledger import ledger_tasks

        doc = make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        ctx = core.Context(doc["_meta"], 1_000, SLUG)
        task = doc["tasks"][0]
        change = op(
            "task_update",
            1,
            item="tasks/t1",
            fields={
                "state": "pr",
                "claimed_by": "owner",
                "pr_url": "https://github.com/o/r/pull/1",
                "awaiting": "merge",
            },
            if_match={"claimed_by": "", "claim_generation": 0, "pr_url": "", "awaiting": ""},
        )
        ledger_tasks.check(change)
        self.assertTrue(ledger_tasks.apply(doc, change, ctx))
        self.assertEqual(task["claim_generation"], 1)
        for key in ("claimed_by", "pr_url", "awaiting"):
            stale = op(
                "task_update",
                2,
                item="tasks/t1",
                fields={"state": "open", "claimed_by": ""},
                if_match={key: "different"},
            )
            ledger_tasks.check(stale)
            self.assertTrue(ledger_tasks.apply(doc, stale, ctx))
            self.assertEqual(task["state"], "pr")
            self.assertEqual(task["claim_generation"], 1)
        fresh = op(
            "task_update",
            3,
            item="tasks/t1",
            fields={"state": "open", "claimed_by": ""},
            if_match={
                "claimed_by": "owner",
                "claim_generation": 1,
                "pr_url": "https://github.com/o/r/pull/1",
                "awaiting": "merge",
            },
        )
        ledger_tasks.check(fresh)
        self.assertTrue(ledger_tasks.apply(doc, fresh, ctx))
        self.assertEqual((task["state"], task["claimed_by"], task["claim_generation"]), ("open", "", 2))

    def test_the_operation_validator_rejects_malformed_ownership_guards(self):
        from scripts.swarm_ledger import ledger_tasks

        for expected in (None, [], "owner", {"unknown": ""}):
            change = op("task_update", 1, item="tasks/t1", fields={"state": "open"}, if_match=expected)
            with self.assertRaisesRegex(ValueError, "^if_match must contain task ownership fields$"):
                ledger_tasks.check(change)
        for key in ("claimed_by", "pr_url", "awaiting"):
            change = op("task_update", 2, item="tasks/t1", fields={"state": "open"}, if_match={key: 1})
            with self.assertRaisesRegex(ValueError, "^if_match ownership fields must be strings$"):
                ledger_tasks.check(change)
        for generation in (None, -1, True, "1", 1.5):
            change = op(
                "task_update", 3, item="tasks/t1", fields={"state": "open"}, if_match={"claim_generation": generation}
            )
            with self.assertRaisesRegex(ValueError, "^if_match claim_generation must be a nonnegative integer$"):
                ledger_tasks.check(change)


class TaskCli(unittest.TestCase):
    def test_task_commands_send_task_ops(self):
        import ledger

        sent = []
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, **f: sent.append((kind, f))):
            for argv in (
                [
                    "--slug",
                    SLUG,
                    "--as",
                    "liaison",
                    "task",
                    "add",
                    "t3",
                    "speed",
                    "up",
                    "--lane",
                    "ci",
                    "--phase",
                    "p1",
                ],
                ["--slug", SLUG, "--as", "liaison", "task", "set", "t3", "state=claimed", "claimed_by=smoke-ci-1"],
            ):
                args = ledger.build_parser().parse_args(argv)
                ledger.cmd_task(args)
        self.assertEqual(
            sent[0], ("task_add", {"task": "t3", "title": "speed up", "lane": "ci", "phase": "p1", "description": ""})
        )
        self.assertEqual(
            sent[1], ("task_update", {"item": "tasks/t3", "fields": {"state": "claimed", "claimed_by": "smoke-ci-1"}})
        )


class TaskRules(unittest.TestCase):
    def test_links_must_be_http(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x"):
            with self.assertRaises(ValueError):
                core.check_op(op("task_update", 9, item="tasks/t1", fields={"pr_url": bad}))
        core.check_op(op("task_update", 9, item="tasks/t1", fields={"pr_url": "https://github.com/o/r/pull/1"}))

    def test_a_task_id_must_be_a_string(self):
        with self.assertRaises(ValueError):
            core.check_op(op("task_add", 8, task=5, title="x", lane="eng"))

    def test_seed_tasks_obey_lane_state_and_link_rules(self):
        base = {"title": "t", "phases": [], "questions": [], "followups": []}
        for task in ({"lane": "ops"}, {"state": "finished"}, {"pr_url": "javascript:x"}):
            with self.assertRaises(ValueError):
                core.validate({**base, "tasks": [{"id": "t1", "title": "a", **task}]})
        core.validate({**base, "tasks": [{"id": "t1", "title": "a", "lane": "ci", "state": "pr"}]})

    def test_a_new_ledger_refuses_an_unknown_lane(self):
        errors = new_ledger.check_types({"title": "t", "tasks": [{"title": "a", "lane": "ops"}]})
        self.assertIn("tasks[1].lane must be eng or ci", errors)


class TaskDependencies(unittest.TestCase):
    def test_task_add_stores_dependencies_and_territory(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        add = op("task_add", 1, task="t2", title="b", lane="eng", depends_on=["t1"], territory=["scripts/swarm"])
        core.check_op(add)
        state, rejected = core.sync(SLUG, ops=[add])
        self.assertEqual(rejected, [])
        self.assertEqual((state["tasks"][1]["depends_on"], state["tasks"][1]["territory"]), (["t1"], ["scripts/swarm"]))

    def test_a_dependency_on_an_unknown_task_is_refused(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        add = op("task_add", 1, task="t2", title="b", lane="eng", depends_on=["t9"])
        state, rejected = core.sync(SLUG, ops=[add])
        self.assertEqual(rejected, [add["id"]])
        self.assertEqual([t["id"] for t in state["tasks"]], ["t1"])
        update = op("task_update", 2, item="tasks/t1", fields={"depends_on": ["t9"]})
        state, rejected = core.sync(SLUG, ops=[update])
        self.assertEqual(rejected, [update["id"]])
        self.assertNotIn("t9", state["tasks"][0].get("depends_on", []))

    def test_a_task_cannot_depend_on_itself(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}])
        update = op("task_update", 1, item="tasks/t1", fields={"depends_on": ["t1"]})
        state, rejected = core.sync(SLUG, ops=[update])
        self.assertEqual(rejected, [update["id"]])
        self.assertEqual(state["tasks"][0].get("depends_on", []), [])

    def test_task_update_sets_dependencies_and_territory(self):
        make_ledger([{"title": "a", "phase": "p1", "lane": "eng"}, {"title": "b", "phase": "p1", "lane": "eng"}])
        update = op("task_update", 1, item="tasks/t2", fields={"depends_on": ["t1"], "territory": ["hooks", "docs"]})
        core.check_op(update)
        state, rejected = core.sync(SLUG, ops=[update])
        self.assertEqual(rejected, [])
        self.assertEqual((state["tasks"][1]["depends_on"], state["tasks"][1]["territory"]), (["t1"], ["hooks", "docs"]))

    def test_dependencies_and_territory_must_be_lists_of_strings(self):
        for bad in (
            op("task_add", 1, task="t2", title="b", lane="eng", depends_on="t1"),
            op("task_add", 2, task="t2", title="b", lane="eng", territory=[3]),
            op("task_update", 3, item="tasks/t1", fields={"territory": "hooks"}),
            op("task_update", 4, item="tasks/t1", fields={"state": ["open"]}),
        ):
            with self.assertRaises(ValueError):
                core.check_op(bad)
        base = {"title": "t", "phases": [], "questions": [], "followups": []}
        with self.assertRaises(ValueError):
            core.validate({**base, "tasks": [{"id": "t1", "title": "a", "depends_on": "t0"}]})

    def test_task_cli_takes_dependencies_and_territory_as_comma_lists(self):
        import ledger

        sent = []
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, **f: sent.append((kind, f))):
            for argv in (
                ["task", "add", "t3", "b", "--depends-on", "t1,t2", "--territory", "scripts/swarm, ledger page"],
                ["task", "set", "t3", "depends_on=t1", "territory="],
            ):
                ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "liaison", *argv]))
        self.assertEqual(sent[0][1]["depends_on"], ["t1", "t2"])
        self.assertEqual(sent[0][1]["territory"], ["scripts/swarm", "ledger page"])
        self.assertEqual(sent[1][1]["fields"], {"depends_on": ["t1"], "territory": []})

    def test_task_add_keeps_the_gain_a_task_promises(self):
        make_ledger()
        add = op("task_add", 1, task="t2", title="b", lane="ci", gain=0.2)
        core.check_op(add)
        state, rejected = core.sync(SLUG, ops=[add, op("task_add", 2, task="t3", title="c", lane="ci")])
        self.assertEqual(rejected, [])
        self.assertEqual([t.get("gain") for t in state["tasks"]], [0.2, None])
        for bad in (-1, "fast", True):
            with self.assertRaises(ValueError):
                core.check_op(op("task_add", 3, task="t4", title="d", lane="ci", gain=bad))

    def test_task_cli_sends_the_gain(self):
        import ledger

        sent = []
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, **f: sent.append((kind, f))):
            for argv in (["task", "add", "t3", "b", "--gain", "1.5"], ["task", "add", "t4", "c"]):
                ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "liaison", *argv]))
        self.assertEqual([f.get("gain") for _, f in sent], [1.5, None])


class TaskBlockersOnThePage(unittest.TestCase):
    def blockers(self, tasks):
        import json
        import subprocess

        page = page_source()
        source = "function taskBlockers(" + page.split("  function taskBlockers(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        script = f"{source}\nconst ts = {json.dumps(tasks)};\nprocess.stdout.write(JSON.stringify(ts.map((t) => taskBlockers(t, ts))));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_a_waiting_task_names_its_blockers_in_plain_words(self):
        tasks = [
            {"id": "t1", "title": "Build the inbox", "state": "open"},
            {"id": "t2", "title": "Speed up the tick", "state": "claimed", "territory": ["scripts/swarm"]},
            {
                "id": "t3",
                "title": "Deliver messages",
                "state": "open",
                "depends_on": ["t1"],
                "territory": ["scripts/swarm/tick.py"],
            },
            {"id": "t4", "title": "Docs", "state": "open"},
        ]
        self.assertEqual(
            self.blockers(tasks),
            [
                "",
                "",
                "Waiting until Build the inbox is done",
                "",
            ],
        )
