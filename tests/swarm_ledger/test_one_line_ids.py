import json
import re
import subprocess
import unittest
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source
from tests.swarm_ledger.test_flat_buttons import css_rules, declarations

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
PROSE_CELLS = {".sw-table .sw-empty td", "#health-table td:nth-child(3)"}
FAKE_DOM = """
const document = { createElement: (tag) => ({ tag, attrs: {}, kids: [],
  setAttribute(k, v) { this.attrs[k] = v; }, append(k) { this.kids.push(k); }, addEventListener() {},
  set textContent(v) { this.text = v; } }) };
"""


def page():
    return page_source()


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def desktop(media):
    return not any("max-width" in m or "hover: none" in m for m in media)


def style():
    return "\n".join(re.findall(r"<style>(.*?)</style>", page(), re.S))


class OneLineIds(unittest.TestCase):
    def test_no_table_cell_wraps_at_desktop_width_except_the_prose_cells(self):
        for media, selector, body in css_rules(style()):
            if not desktop(media):
                continue
            for part in (p.strip() for p in selector.split(",")):
                if re.search(r"-table\b.*\btd\b", part) and part not in PROSE_CELLS:
                    decl = declarations(body)
                    self.assertNotEqual(decl.get("white-space"), "normal", part)
                    self.assertNotIn("overflow-wrap", decl, part)

    def test_an_id_stays_on_one_line_and_truncates_with_an_ellipsis(self):
        rule = next(declarations(b) for m, s, b in css_rules(style()) if s == ".sw-id" and desktop(m))
        self.assertEqual(rule.get("white-space"), "nowrap")
        self.assertEqual(rule.get("overflow"), "hidden")
        self.assertEqual(rule.get("text-overflow"), "ellipsis")
        self.assertIn("max-width", rule)

    def test_an_id_cell_carries_its_full_name_as_a_tooltip(self):
        script = (
            FAKE_DOM
            + function_source("h")
            + "\n"
            + function_source("idCell")
            + '\nprocess.stdout.write(JSON.stringify(idCell("engineer@323133-0085")));'
        )
        out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out["attrs"], {"class": "sw-id", "title": "engineer@323133-0085"})
        self.assertEqual(out["text"], "engineer@323133-0085")

    def test_agent_quota_health_and_seat_names_render_as_id_cells(self):
        render = (
            function_source("agentRow")
            + function_source("quotaRow")
            + function_source("healthRow")
            + function_source("renderHandoffs")
        )
        for value in ("a.name", "q.account", "f.subject", "seatText(r.seat)", "r.successor"):
            self.assertIn(f"idCell({value})", render, value)


if __name__ == "__main__":
    unittest.main()
