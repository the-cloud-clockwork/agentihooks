import sys
import unittest
from pathlib import Path

from tests.swarm_ledger.ledger_page import browser_home, page_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "title-2026-01-01"


def make_ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
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
        from scripts.swarm_ledger.repository import repository

        state, rejected = rename("  Renamed plan  ")
        self.assertEqual(rejected, [])
        self.assertEqual(state["title"], "Renamed plan")
        self.assertEqual(core.sync(SLUG)[0]["title"], "Renamed plan")
        self.assertEqual(repository.get_document(SLUG)["title"], "Renamed plan")
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
        self.assertIn("Fresh name", browser_home(server))
        self.assertIn(f'href="/{SLUG}"', browser_home(server))


class TitleHeader(unittest.TestCase):
    page = page_source()

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
