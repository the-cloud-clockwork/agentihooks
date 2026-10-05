import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import new_ledger  # noqa: E402

TEMPLATE = SCRIPTS / "template.html"
CONTENT = {
    "title": "Demo",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "Build it", "description": "d"}, {"title": "Ship it", "description": "d"}],
    "questions": [{"text": "Which port?"}],
    "followups": [{"text": "Rotate the key"}],
    "tasks": [{"title": "Write the test", "description": "", "phase": "p1", "lane": "eng"}],
}
HEADS = [
    {"id": "sec-overview", "title": "Overview", "list": ""},
    {"id": "sec-phases", "title": "Plan phases", "list": "phases"},
    {"id": "sec-tasks", "title": "Swarm tasks", "list": "tasks"},
    {"id": "sec-questions", "title": "Open questions", "list": "questions"},
    {"id": "sec-followups", "title": "Follow-ups or blockers", "list": "followups"},
]


def function_source(name):
    page = TEMPLATE.read_text(encoding="utf-8")
    return f"function {name}(" + page.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


class Outline(unittest.TestCase):
    def test_outline_lists_each_section_and_its_item_titles(self):
        doc = new_ledger.build_doc(CONTENT)
        script = (
            function_source("itemState")
            + "\n"
            + function_source("outlineOf")
            + f"\nprocess.stdout.write(JSON.stringify(outlineOf({json.dumps(doc)}, {json.dumps(HEADS)})));"
        )
        out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(
            out,
            [
                {"id": "sec-overview", "title": "Overview", "items": []},
                {
                    "id": "sec-phases",
                    "title": "Plan phases",
                    "items": [
                        {"id": "item-phases-p1", "title": "Build it", "state": "open"},
                        {"id": "item-phases-p2", "title": "Ship it", "state": "open"},
                    ],
                },
                {
                    "id": "sec-tasks",
                    "title": "Swarm tasks",
                    "items": [{"id": "item-tasks-t1", "title": "Write the test", "state": "open"}],
                },
                {
                    "id": "sec-questions",
                    "title": "Open questions",
                    "items": [{"id": "item-questions-q1", "title": "Which port?", "state": "open"}],
                },
                {
                    "id": "sec-followups",
                    "title": "Follow-ups or blockers",
                    "items": [{"id": "item-followups-f1", "title": "Rotate the key", "state": "open"}],
                },
            ],
        )

    def test_every_content_section_is_tagged_for_the_outline(self):
        page = TEMPLATE.read_text(encoding="utf-8")
        column = page.split('<div class="layout"><div class="col">', 1)[1].split("<aside", 1)[0]
        sections = re.findall(r"<section[^>]*>", column)
        self.assertTrue(sections)
        for tag in sections:
            self.assertRegex(tag, r'id="[^"]+"')
            self.assertIn("data-outline", tag)
        for list_name in ("sources", "priorities", "phases", "tasks", "questions", "notes", "followups"):
            self.assertIn(f'data-outline="{list_name}"', column)

    def test_outline_nav_and_narrow_toggle_exist_and_render_refreshes_it(self):
        page = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('<nav class="outline" id="outline"', page)
        self.assertIn('id="outline-toggle"', page)
        self.assertIn("renderOutline();", function_source("render"))

    def test_wide_layout_offsets_the_content_and_keeps_the_button_stack_clear(self):
        page = TEMPLATE.read_text(encoding="utf-8")
        wide = page.split("@media (min-width: 1280px) {", 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(wide, r"body \{[^}]*padding-left: var\(--outline-w\)")
        self.assertRegex(page, r"\.outline \{[^}]*width: calc\(var\(--outline-w\) - \d+px\)")
        bottom = int(re.search(r"\.outline \{[^}]*bottom: (\d+)px", page).group(1))
        home = int(re.search(r"\.fab-home \{ bottom: (\d+)px", page).group(1))
        self.assertGreater(bottom, home + 46)


if __name__ == "__main__":
    unittest.main()
