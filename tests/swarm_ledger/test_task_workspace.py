import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402

from tests.swarm_ledger.test_task_kinds import SLUG, make_ledger, op  # noqa: E402
from tests.swarm_ledger.test_task_proof_page import PLAIN, render, texts  # noqa: E402


class WorkspaceField(unittest.TestCase):
    def test_task_add_and_update_store_the_work_folder(self):
        state = make_ledger([{"task": "t1", "title": "a", "lane": "eng", "workspace": "/w/t1"}])
        self.assertEqual(state["tasks"][0]["workspace"], "/w/t1")
        state, rejected = core.sync(SLUG, ops=[op("task_update", 1, item="tasks/t1", fields={"workspace": "/w/t1b"})])
        self.assertEqual((rejected, state["tasks"][0]["workspace"]), ([], "/w/t1b"))

    def test_a_workspace_that_is_not_a_string_is_refused(self):
        with self.assertRaises(ValueError):
            core.check_op(op("task_add", 1, task="t1", title="a", lane="eng", workspace=["/w"]))

    def test_task_add_scaffold_creates_the_folder_in_the_same_call(self):
        import ledger

        sent = []
        argv = ["--slug", SLUG, "--as", "liaison", "task", "add", "t9", "build", "it", "--description", "seams"]
        with unittest.mock.patch.object(ledger, "send", lambda args, kind, /, **f: sent.append(f)):
            ledger.cmd_task(ledger.build_parser().parse_args([*argv, "--scaffold"]))
            ledger.cmd_task(ledger.build_parser().parse_args(argv[:6] + ["t10", "plain"]))
        folder = Path.home() / ".agentihooks" / "swarm" / SLUG / "tasks" / "t9"
        self.assertEqual(sent[0]["workspace"], str(folder))
        self.assertIn("seams", (folder / "steering.md").read_text(encoding="utf-8"))
        self.assertNotIn("workspace", sent[1])


class WorkspaceOnThePage(unittest.TestCase):
    def test_the_reply_carries_the_latest_lines_of_tasks_with_a_work_folder(self):
        from unittest.mock import patch

        tail = {"latest_progress": "red test seen\ngreen now"}
        state = {"tasks": [{"id": "t1", "workspace": "/remote/task"}, {"id": "t2"}]}
        with patch.object(server, "workspace_tails", return_value={"t1": tail}):
            tasks = server.with_workspaces("pg", state)["tasks"]
        self.assertEqual(tasks[0]["workspace_tail"], tail)
        self.assertNotIn("workspace_tail", tasks[1])
        self.assertNotIn("workspace_tail", state["tasks"][0])

    def test_the_proof_fold_renders_progress_and_proof(self):
        tail = {"latest_progress": "green now", "latest_proof": "run 7 passed"}
        shown = texts(render({**PLAIN, "workspace": "/w/t1", "workspace_tail": tail})["tree"])
        for text in ("Contract and proof", "Latest progress", "green now", "Latest proof", "run 7 passed"):
            self.assertIn(text, shown)


if __name__ == "__main__":
    unittest.main()
