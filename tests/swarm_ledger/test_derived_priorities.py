import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "derived-prio-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "Which broker should the gateway use?"}],
        "followups": [],
        "tasks": [{"title": "Wire the broker", "phase": "p1", "lane": "eng"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def agent(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "boss", **fields}


def items(state):
    return {p["item"]: p["text"] for p in state["priorities"]}


class DerivedPriorities(unittest.TestCase):
    def setUp(self):
        state = make_ledger()
        self.question = f"questions/{state['questions'][0]['id']}"
        self.phase = f"phases/{state['phases'][0]['id']}"
        self.task = f"tasks/{state['tasks'][0]['id']}"

    def test_an_open_question_is_a_priority_until_the_operator_answers_it(self):
        state, _ = core.sync(SLUG)
        self.assertIn("Which broker should the gateway use?", items(state)[self.question])
        answer = {"op": "add", "id": "a1", "thread": f"{self.question}/answers", "text": "the second one"}
        state, _ = core.sync(SLUG, ops=[answer])
        self.assertNotIn(self.question, items(state))

    def test_a_blocked_task_is_a_priority_with_its_reason_until_it_unblocks(self):
        reason = agent("add", 1, thread=f"{self.task}/comments", text="Needs a token only the operator can create.")
        block = agent("task_update", 2, item=self.task, fields={"state": "blocked"})
        state, _ = core.sync(SLUG, ops=[reason, block])
        self.assertIn("Needs a token only the operator can create.", items(state)[self.task])
        state, _ = core.sync(SLUG, ops=[agent("task_update", 3, item=self.task, fields={"state": "claimed"})])
        self.assertNotIn(self.task, items(state))

    def test_a_pull_request_awaiting_approval_is_a_priority_until_the_task_moves_on(self):
        fields = {"state": "pr", "pr_url": "https://github.com/o/r/pull/1", "awaiting": "approval"}
        state, rejected = core.sync(SLUG, ops=[agent("task_update", 4, item=self.task, fields=fields)])
        self.assertEqual(rejected, [])
        self.assertIn("Wire the broker", items(state)[self.task])
        state, _ = core.sync(SLUG, ops=[agent("task_update", 5, item=self.task, fields={"state": "claimed"})])
        self.assertNotIn(self.task, items(state))

    def test_a_pull_request_without_an_approval_wait_is_no_priority(self):
        fields = {"state": "pr", "pr_url": "https://github.com/o/r/pull/1"}
        state, _ = core.sync(SLUG, ops=[agent("task_update", 6, item=self.task, fields=fields)])
        self.assertNotIn(self.task, items(state))

    def test_a_flagged_follow_up_is_a_priority_until_closed_or_unflagged(self):
        add = agent("add_item", 7, list="followups", text="Keep the old alias or drop it?", needs_operator=True)
        state, rejected = core.sync(SLUG, ops=[add])
        self.assertEqual(rejected, [])
        self.assertIn("Keep the old alias or drop it?", items(state)["followups/add_item-7"])
        state, _ = core.sync(SLUG, ops=[agent("set", 8, path="followups/add_item-7/done", value=True)])
        self.assertNotIn("followups/add_item-7", items(state))
        core.sync(SLUG, ops=[agent("set", 9, path="followups/add_item-7/done", value=False)])
        state, _ = core.sync(SLUG)
        self.assertIn("followups/add_item-7", items(state))
        state, _ = core.sync(SLUG, ops=[agent("set", 10, path="followups/add_item-7/needs_operator", value=False)])
        self.assertNotIn("followups/add_item-7", items(state))

    def test_an_unflagged_follow_up_is_no_priority(self):
        state, _ = core.sync(SLUG, ops=[agent("add_item", 11, list="followups", text="Tidy the logs later.")])
        self.assertNotIn("followups/add_item-11", items(state))

    def test_a_manual_priority_survives_and_wins_over_a_derived_one(self):
        manual = agent("priority", 12, item=self.phase, text="Approve the broker choice so it can merge.")
        on_question = agent("priority", 13, item=self.question, text="Pick the broker today.")
        core.sync(SLUG, ops=[manual, on_question])
        answer = {"op": "add", "id": "a2", "thread": f"{self.question}/answers", "text": "first"}
        state, _ = core.sync(SLUG, ops=[answer])
        self.assertEqual(items(state)[self.phase], "Approve the broker choice so it can merge.")
        self.assertEqual(items(state)[self.question], "Pick the broker today.")
        self.assertEqual(len(state["priorities"]), 2)

    def test_an_agent_priority_may_point_at_a_task_and_the_operator_clears_it(self):
        _, ops = core.check_body({"ops": [agent("priority", 14, item=self.task, text="Merge this one first.")]})
        state, rejected = core.sync(SLUG, ops=ops)
        self.assertEqual(rejected, [])
        self.assertEqual(items(state)[self.task], "Merge this one first.")
        state, _ = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "c1", "target": "priority-14"}])
        self.assertNotIn(self.task, items(state))

    def test_a_derived_priority_the_operator_clears_stays_cleared_until_it_changes(self):
        state, _ = core.sync(SLUG)
        row = next(p for p in state["priorities"] if p["item"] == self.question)
        state, _ = core.sync(SLUG, ops=[{"op": "priority_clear", "id": "c2", "target": row["id"]}])
        self.assertNotIn(self.question, items(state))
        state, _ = core.sync(SLUG)
        self.assertNotIn(self.question, items(state))
        retext = agent("retext", 15, item=self.question, text="Which broker, the first or the second?")
        state, _ = core.sync(SLUG, ops=[retext])
        self.assertIn(self.question, items(state))


class FollowupFlagCli(unittest.TestCase):
    def sent(self, *argv):
        import ledger

        calls = []
        args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "boss", "followup", *argv])
        with unittest.mock.patch.object(ledger, "send", lambda a, kind, **f: calls.append((kind, f))):
            ledger.cmd_followup(args)
        return calls

    def test_add_with_needs_operator_flags_the_follow_up(self):
        calls = self.sent("add", "Keep the alias?", "--needs-operator")
        self.assertEqual(
            calls, [("add_item", {"list": "followups", "text": "Keep the alias?", "needs_operator": True})]
        )

    def test_flag_and_unflag_set_the_flag(self):
        self.assertEqual(self.sent("flag", "f1"), [("set", {"path": "followups/f1/needs_operator", "value": True})])
        self.assertEqual(self.sent("unflag", "f1"), [("set", {"path": "followups/f1/needs_operator", "value": False})])


if __name__ == "__main__":
    unittest.main()
