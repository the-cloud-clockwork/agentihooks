import contextlib
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

from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import chromium, rendered_home  # noqa: E402

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
            html_path.write_text(legacy_page.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")
        cls.browsers = contextlib.ExitStack()
        cls.browser = cls.browsers.enter_context(chromium())

    @classmethod
    def tearDownClass(cls):
        cls.browsers.close()

    def test_summaries_discover_every_ledger_in_the_store(self):
        found = {s["slug"]: s for s in server.ledger_summaries()}
        self.assertLessEqual(set(LEDGERS), set(found))
        self.assertEqual(found["alpha-2026-01-01"]["title"], "Alpha plan")
        self.assertEqual(found["alpha-2026-01-01"]["overview"], "Alpha overview")

    def test_index_lists_name_overview_and_link_escaped(self):
        page = rendered_home(server, self.browser, "home")
        for slug, (title, overview) in LEDGERS.items():
            self.assertIn(f'href="/{slug}"', page)
            self.assertIn(overview, page)
        self.assertIn("Beta &lt;plan&gt;", page)
        self.assertNotIn("Beta <plan>", page)

    def test_ledger_page_has_home_link_above_the_bell(self):
        shell = core.SHELL.read_text(encoding="utf-8")
        home, bell = shell.index('id="home"'), shell.index('id="bell"')
        self.assertLess(home, bell)
        self.assertIn("fab-home", shell)
        self.assertIn(".fab-home", (SCRIPTS / "static" / "css" / "ledger.css").read_text(encoding="utf-8"))

    def test_home_and_bin_render_from_the_home_html_source(self):
        source = core.HOME.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp) / "home.html"
            edited.write_text(
                source.replace("</title>", '</title><p id="edited">__HOME_HEADING__</p>'), encoding="utf-8"
            )
            with mock.patch.object(core, "HOME", edited):
                self.assertIn('<p id="edited">HOME</p>', rendered_home(server, self.browser, "home"))
                self.assertIn('<p id="edited">BIN</p>', rendered_home(server, self.browser, "bin"))
        self.assertTrue(source.rstrip().endswith("</html>"))


if __name__ == "__main__":
    unittest.main()
