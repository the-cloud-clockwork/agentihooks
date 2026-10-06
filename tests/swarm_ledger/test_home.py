import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_bin  # noqa: E402
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


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", server.HOME_STYLE).group(1)


def task_op(n, **fields):
    return {
        "op": "task_add",
        "id": f"add-{n}",
        "by": "row-engineer",
        "task": f"t{n}",
        "title": f"Task {n}",
        "lane": "eng",
        **fields,
    }


class Rows(unittest.TestCase):
    SLUG = "rows-2026-01-03"

    @classmethod
    def setUpClass(cls):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        content = {"title": "Rows plan", "overview": "A long overview " * 20, "sources": [], "phases": []}
        content.update(questions=[], followups=[])
        html_path, _ = core.paths(cls.SLUG)
        html_path.write_text(
            new_ledger.render(new_ledger.build_doc(content, "swarm"), cls.SLUG, 8765), encoding="utf-8"
        )
        done = {
            "op": "task_update",
            "id": "up-3",
            "by": "row-engineer",
            "item": "tasks/t3",
            "fields": {"state": "done"},
        }
        state, rejected = core.sync(cls.SLUG, ops=[task_op(1), task_op(2), task_op(3), done])
        assert rejected == [], rejected
        cls.updated_at = state["_meta"]["updated_at"]

    def setUp(self):
        ledger_bin.bin_path().unlink(missing_ok=True)
        states = patch.object(server, "swarm_state", side_effect=lambda slug: "running" if slug == self.SLUG else None)
        states.start()
        self.addCleanup(states.stop)

    def row(self, page, slug):
        return re.search(rf'<li class="row"[^>]*>(?:(?!</li>).)*href="/{slug}"(?:(?!</li>).)*</li>', page, re.S).group(
            0
        )

    def test_home_spans_the_full_window_width(self):
        self.assertNotIn("max-width", rule("main"))
        self.assertNotIn("max-width", rule("ul"))

    def test_each_row_carries_title_kind_counts_swarm_state_and_last_activity(self):
        row = self.row(server.index_page(now=self.updated_at + 5 * 60 * 1000), self.SLUG)
        self.assertIn(">Rows plan</a>", row)
        self.assertIn('<span class="kind">swarm</span>', row)
        self.assertIn('<span class="num open"><b>2</b> open</span>', row)
        self.assertIn('<span class="num done"><b>1</b> done</span>', row)
        self.assertIn('<span class="state s-running">running</span>', row)
        self.assertRegex(row, r'<time class="when" datetime="[^"]+" title="[^"]+">5m ago</time>')
        self.assertRegex(row, r'<span class="ov" title="A long overview[^"]*">A long overview')

    def test_a_ledger_without_a_swarm_says_so_in_its_row(self):
        row = self.row(server.index_page(), "alpha-2026-01-01")
        self.assertIn('<span class="state s-none">no swarm</span>', row)

    def test_a_row_stays_on_one_line_and_truncates_the_overview(self):
        self.assertIn("grid-template-columns", rule(".row"))
        self.assertIn("white-space:nowrap", rule(".row"))
        for declaration in ("overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
            self.assertIn(declaration, rule(".ov"))

    def test_buttons_and_the_floating_bin_entry_are_flat_at_rest(self):
        for selector in (".act", ".fab"):
            self.assertIn("background:transparent", rule(selector))
            self.assertIn("border:0", rule(selector))

    def test_the_bin_lists_days_left_and_a_restore_button(self):
        ledger_bin.delete(self.SLUG)
        self.assertNotIn(f'href="/{self.SLUG}"', server.index_page())
        row = self.row(server.index_page(view="bin"), self.SLUG)
        self.assertIn('<span class="left">30 days left</span>', row)
        self.assertIn(f'data-act="restore" data-slug="{self.SLUG}"', row)
        self.assertIn("Restore</button>", row)


if __name__ == "__main__":
    unittest.main()
