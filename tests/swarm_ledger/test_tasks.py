import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
os.environ["LEDGER_DIR"] = tempfile.mkdtemp(prefix="ledger-tasks-test-")
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "tasks-2026-01-01"


def make_ledger(tasks=()):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "tasks": list(tasks),
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
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
