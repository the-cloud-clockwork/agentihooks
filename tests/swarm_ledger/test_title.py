import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("LEDGER_DIR", tempfile.mkdtemp(prefix="ledger-title-test-"))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "title-2026-01-01"


def make_ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def rename(text, n=1):
    op = {"op": "title_set", "id": f"title-{n}", "text": text}
    core.check_body({"ops": [op]})
    return core.sync(SLUG, ops=[op])


class TitleOp(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_stores_the_trimmed_title_and_keeps_the_slug(self):
        state, rejected = rename("  Renamed plan  ")
        self.assertEqual(rejected, [])
        self.assertEqual(state["title"], "Renamed plan")
        self.assertEqual(core.sync(SLUG)[0]["title"], "Renamed plan")
        html_path, json_path = core.paths(SLUG)
        self.assertTrue(html_path.exists() and json_path.exists())
        self.assertEqual(core.parse_seed(html_path.read_text(encoding="utf-8"))["title"], "Renamed plan")
        self.assertEqual(state["_meta"]["events"][-1]["kind"], "title changed")

    def test_refuses_an_empty_or_whitespace_title(self):
        for text in ("", "   \n\t"):
            with self.assertRaises(ValueError):
                rename(text)
        self.assertEqual(core.sync(SLUG)[0]["title"], "Demo")

    def test_refuses_a_non_operator_sender(self):
        op = {"op": "title_set", "id": "t-agent", "text": "Agent title", "by": "swarm-buildout-eng-1"}
        with self.assertRaises(ValueError):
            core.check_body({"ops": [op]})

    def test_home_lists_the_new_title(self):
        rename("Fresh name")
        found = {s["slug"]: s for s in server.ledger_summaries()}
        self.assertEqual(found[SLUG]["title"], "Fresh name")
        self.assertIn("Fresh name", server.index_page())
        self.assertIn(f'href="/{SLUG}"', server.index_page())


class TitleHeader(unittest.TestCase):
    page = (SCRIPTS / "template.html").read_text(encoding="utf-8")

    def test_pen_button_sits_next_to_the_title(self):
        self.assertIn('id="title-edit"', self.page)
        self.assertLess(self.page.index('id="title"'), self.page.index('id="title-edit"'))
        self.assertRegex(self.page, r"\.title-pen \{[^}]*background: none;[^}]*border: 0;")

    def test_page_edits_saves_and_cancels_the_title(self):
        self.assertIn('op: "title_set"', self.page)
        self.assertIn('ev.key === "Escape"', self.page)
        self.assertIn('ev.key === "Enter"', self.page)
        self.assertIn('id="title-error"', self.page)
        self.assertIn("document.title = doc.title", self.page)


if __name__ == "__main__":
    unittest.main()
