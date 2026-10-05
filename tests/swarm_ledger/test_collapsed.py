import json
import re
import subprocess
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"


def page():
    return TEMPLATE.read_text(encoding="utf-8")


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def run_js(names, expr):
    script = "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def details(box_id):
    match = re.search(rf'<details([^>]*) id="{box_id}"([^>]*)>(.*?)</details>', page(), re.S)
    return match.group(1) + match.group(2), match.group(3)


class CollapsedByDefault(unittest.TestCase):
    def test_swarm_tasks_start_collapsed_with_their_counts_in_the_header(self):
        attrs, body = details("tasks-box")
        self.assertNotIn("open", attrs)
        summary = body.split("</summary>", 1)[0]
        self.assertIn("Swarm tasks", summary)
        self.assertIn('id="tasks-count"', summary)
        self.assertIn('<ol id="tasks">', body)

    def test_swarm_agents_start_collapsed_with_their_count_in_the_header(self):
        attrs, body = details("swarm-agents-box")
        self.assertIn("open", attrs)
        self.assertIn('id="swarm-agents-count"', body.split("</summary>", 1)[0])
        self.assertIn('id="swarm-agents"', body)
        self.assertIn('$("swarm-agents-count").textContent', function_source("renderWork"))

    def test_task_counts_by_state_in_a_fixed_order_skipping_empty_states(self):
        tasks = [{"state": s} for s in ("done", "open", "claimed", "done", "pr", "done", "open")]
        self.assertEqual(
            run_js(["taskCounts", "headCount"], f"taskCounts({json.dumps(tasks)})"),
            "· 2 open · 1 claimed · 1 pr · 3 done",
        )
        self.assertEqual(run_js(["taskCounts", "headCount"], "taskCounts([])"), "")
        self.assertIn('$("tasks-count").textContent = taskCounts(doc.tasks)', function_source("render"))

    def test_an_outline_jump_opens_the_collapsed_section_holding_its_target(self):
        expr = """(() => {
  const box = { open: false, parentElement: { closest: () => null } };
  const inside = { closest: (sel) => (sel === "details" ? box : null) };
  reveal(inside);
  const loose = { closest: () => null };
  reveal(loose);
  return box.open;
})()"""
        self.assertTrue(run_js(["reveal"], expr))
        click = page().split('$("outline").addEventListener("click"', 1)[1].split("});", 1)[0]
        self.assertLess(click.index("reveal(el)"), click.index("scrollIntoView"))

    def test_hidden_outline_targets_never_count_as_the_section_in_view(self):
        self.assertIn("getClientRects().length", function_source("markOutline"))


if __name__ == "__main__":
    unittest.main()
