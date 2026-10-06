import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

LEDGERS = {"alpha-2026-01-01": ("Alpha plan", "Alpha overview"), "beta-2026-01-02": ("Beta <plan>", "Beta overview")}


class Home(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        for slug, (title, overview) in LEDGERS.items():
            content = {
                "title": title,
                "overview": overview,
                "sources": [],
                "phases": [],
                "questions": [],
                "followups": [],
            }
            html_path, _ = core.paths(slug)
            html_path.write_text(new_ledger.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")

    def test_summaries_discover_every_ledger_in_the_store(self):
        found = {s["slug"]: s for s in server.ledger_summaries()}
        self.assertLessEqual(set(LEDGERS), set(found))
        self.assertEqual(found["alpha-2026-01-01"]["title"], "Alpha plan")
        self.assertEqual(found["alpha-2026-01-01"]["overview"], "Alpha overview")

    def test_index_lists_name_overview_and_link_escaped(self):
        page = server.index_page()
        for slug, (title, overview) in LEDGERS.items():
            self.assertIn(f'href="/{slug}"', page)
            self.assertIn(overview, page)
        self.assertIn("Beta &lt;plan&gt;", page)
        self.assertNotIn("Beta <plan>", page)

    def test_ledger_page_has_home_link_above_the_bell(self):
        template = (SCRIPTS / "template.html").read_text(encoding="utf-8")
        home, bell = template.index('id="home"'), template.index('id="bell"')
        self.assertLess(home, bell)
        self.assertIn(".fab-home", template)

    def test_home_and_bin_render_from_the_home_html_source(self):
        source = server.HOME_PAGE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / "home.html"
            edited.write_text(source.replace("</style>", "</style><p id=edited>__HOME_HEADING__</p>"), encoding="utf-8")
            with mock.patch.object(server, "HOME_PAGE", edited):
                self.assertIn("<p id=edited>HOME</p>", server.index_page("home"))
                self.assertIn("<p id=edited>BIN</p>", server.index_page("bin"))
        self.assertTrue(source.rstrip().endswith("</body>"))


if __name__ == "__main__":
    unittest.main()
