import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.swarm_ledger.ledger_page import browser_home, page_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_close  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "close-2026-01-01"
OPEN_SLUG = "still-open-2026-01-01"
PR = "https://github.com/o/r/pull/7"


def ledger_doc():
    return {
        "overview": "Ship the thing.",
        "phases": [
            {"id": "p1", "title": "One", "done": True},
            {"id": "p2", "title": "Two", "done": False},
            {"id": "p3", "title": "Gone", "done": False, "out_of_scope": True},
        ],
        "tasks": [
            {"id": "t1", "title": "Merged work", "state": "done", "pr_url": PR},
            {"id": "t2", "title": "Half way", "state": "claimed", "pr_url": ""},
            {"id": "t3", "title": "Not started", "state": "open"},
            {"id": "t4", "title": "Dropped", "state": "open", "out_of_scope": True},
        ],
        "followups": [
            {"id": "f1", "text": "Tidy the docs", "done": False},
            {"id": "f2", "text": "Already tidy", "done": True},
        ],
        "questions": [
            {"id": "q1", "text": "Which colour", "answers": []},
            {"id": "q2", "text": "Which size", "answers": [{"id": "a", "by": "operator", "text": "large"}]},
            {"id": "q3", "text": "Withdrawn", "answers": [{"id": "b", "by": "operator", "text": "", "deleted": True}]},
        ],
    }


def make_ledger(slug=SLUG):
    content = {"title": "Demo", "overview": "Ship the thing.", "sources": [], "phases": [], "questions": []}
    content["followups"] = []
    html_path, json_path = core.paths(slug)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(slug)


def apply(op, slug=SLUG):
    core.check_body({"ops": [op]})
    return core.sync(slug, ops=[op])


class SummaryBuilder(unittest.TestCase):
    def test_lists_merged_work_with_links_and_everything_left_open(self):
        text = ledger_close.summary(ledger_doc())
        self.assertTrue(text.startswith("Summary\n"))
        self.assertIn("Phases done 1 of 2.", text)
        self.assertIn(f"Merged:\n- Merged work {PR}", text)
        self.assertIn("Tasks still open:\n- Half way (claimed)\n- Not started (open)", text)
        self.assertIn("Follow ups still open:\n- Tidy the docs", text)
        self.assertIn("Questions unanswered:\n- Which colour\n- Withdrawn", text)
        for left_out in ("Dropped", "Already tidy", "Which size", "Gone"):
            self.assertNotIn(left_out, text)

    def test_empty_lists_say_none_and_the_note_sits_on_top(self):
        text = ledger_close.summary({"phases": [], "tasks": [], "followups": [], "questions": []}, "All shipped.")
        self.assertEqual(
            text,
            "Summary\nAll shipped.\n\nPhases done 0 of 0.\nMerged:\n- none\nTasks still open:\n- none\n"
            "Follow ups still open:\n- none\nQuestions unanswered:\n- none",
        )

    def test_a_new_summary_replaces_the_old_one_and_keeps_the_intro(self):
        once = ledger_close.with_summary("Intro words.", "Summary\nfirst")
        twice = ledger_close.with_summary(once, "Summary\nsecond")
        self.assertEqual(twice, "Intro words.\n\nSummary\nsecond")
        self.assertEqual(ledger_close.intro(twice), "Intro words.")


class CloseOps(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_summary_set_writes_the_summary_built_from_the_ledger_into_the_overview(self):
        apply({"op": "task_add", "id": "a1", "by": "sw-master-1", "task": "t1", "title": "Merged work", "lane": "eng"})
        apply(
            {
                "op": "task_update",
                "id": "a2",
                "by": "sw-eng-1",
                "item": "tasks/t1",
                "fields": {"state": "done", "pr_url": PR},
            }
        )
        state, rejected = apply({"op": "summary_set", "id": "s1", "by": "sw-master-1", "note": "It went well."})
        self.assertEqual(rejected, [])
        self.assertTrue(state["overview"].startswith("Ship the thing.\n\nSummary\nIt went well.\n"))
        self.assertIn(f"- Merged work {PR}", state["overview"])
        self.assertEqual(state["_meta"]["events"][-1]["kind"], "summary written")
        self.assertEqual(state["_meta"]["warnings"], [])

    def test_close_marks_the_ledger_closed_with_when(self):
        state, rejected = apply({"op": "close", "id": "c1", "by": "swarm"})
        self.assertEqual(rejected, [])
        self.assertIsInstance(state["closed_at"], int)
        self.assertEqual(state["_meta"]["events"][-1]["kind"], "ledger closed")
        self.assertEqual(core.sync(SLUG)[0]["closed_at"], state["closed_at"])

    def test_a_long_summary_never_trips_the_overview_word_limit(self):
        state, _ = apply({"op": "summary_set", "id": "s2", "by": "operator", "note": "word " * 300})
        self.assertEqual(state["_meta"]["warnings"], [])

    def test_bad_ops_are_refused(self):
        for op in (
            {"op": "close", "id": "c", "by": "swarm", "extra": 1},
            {"op": "close", "id": "c"},
            {"op": "summary_set", "id": "s", "by": "swarm", "note": 5},
            {"op": "summary_set", "id": "s", "by": "bad name"},
        ):
            with self.assertRaises(ValueError, msg=op):
                core.check_body({"ops": [op]})


class ClosedPage(unittest.TestCase):
    def run_js(self, expr):
        page = page_source()
        source = "function closedText(" + page.split("  function closedText(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        script = source + f"\nprocess.stdout.write(JSON.stringify({expr}));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_the_closed_banner_names_when_and_hides_while_open(self):
        self.assertEqual(
            self.run_js("[closedText(null), closedText(Date.UTC(2026, 9, 5, 14, 2))]"),
            ["", "Closed 2026-10-05 14:02 UTC"],
        )

    def test_the_page_carries_the_banner_and_renders_it_from_closed_at(self):
        page = page_source()
        self.assertIn('id="closed-banner"', page)
        self.assertIn("closed_at:", page.split("function withDefaults(", 1)[1].split("\n  }\n", 1)[0])
        self.assertIn("closedText(doc.closed_at)", page)


class Home(unittest.TestCase):
    def test_home_shows_a_closed_ledger_not_yet_binned_as_closed_in_its_row(self):
        make_ledger(OPEN_SLUG)
        make_ledger()
        apply({"op": "close", "id": "c2", "by": "swarm"})
        found = {s["slug"]: s for s in server.ledger_summaries()}
        self.assertIsInstance(found[SLUG]["closed_at"], int)
        self.assertIsNone(found[OPEN_SLUG]["closed_at"])
        with patch.object(server, "swarm_state", return_value="stopped"):
            page = browser_home(server)
        self.assertNotIn("<h1>CLOSED</h1>", page)
        rows = {
            slug: row
            for row in page.split('<li class="row"')[1:]
            for slug in (SLUG, OPEN_SLUG)
            if f'href="/{slug}"' in row
        }
        self.assertIn('<span class="state s-closed">closed</span>', rows[SLUG])
        self.assertIn('<span class="state s-stopped">stopped</span>', rows[OPEN_SLUG])

    def test_the_page_close_runs_the_same_close_command_as_the_cli(self):
        self.assertEqual(server.control_argv({"action": "close"}), ["close"])


if __name__ == "__main__":
    unittest.main()
