import json
import re
import subprocess
import unittest
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
STUB_H = "function h(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.filter(Boolean) }; }\n"
STUB_FOLD = "function collapsible() {}\n"
STUB_PAGES = (
    "function lazy(box, fill) { box.kids.push(...[fill()].flat().filter(Boolean)); return box; }\n"
    "function firstPage(key, items) { return items; }\n"
    "function moreButton() { return null; }\n"
    "function outlineAgain() {}\n"
)


def page():
    return page_source()


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def run_js(names, expr, prelude=""):
    script = (
        prelude + "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def css_rule(selector):
    css = "\n".join(re.findall(r"<style>(.*?)</style>", page(), re.S))
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    return match and match.group(1)


DOC = {
    "overview": "o",
    "sources": ["a source"],
    "notes": [{"id": "n1", "text": "a note"}],
    "phases": [{"id": "p1", "title": "open phase", "done": False}, {"id": "p2", "title": "done phase", "done": True}],
    "tasks": [
        {"id": "t1", "title": "open task", "state": "claimed", "done": False},
        {"id": "t2", "title": "done task", "state": "done", "done": True},
        {"id": "t3", "title": "parked task", "state": "open", "done": False, "out_of_scope": True},
    ],
    "questions": [
        {"id": "q1", "text": "unanswered", "answers": []},
        {"id": "q2", "text": "answered", "answers": [{"id": "a1", "text": "yes"}]},
        {"id": "q3", "text": "answer deleted", "answers": [{"id": "a2", "text": "x", "deleted": True}]},
        {"id": "q4", "text": "parked", "answers": [], "out_of_scope": True},
    ],
    "followups": [{"id": "f1", "text": "open", "done": False}, {"id": "f2", "text": "parked", "out_of_scope": True}],
}
HEADS = [
    {"id": "sec-overview", "title": "Overview"},
    {"id": "sec-sources", "title": "Sources", "list": "sources"},
    {"id": "sec-notes", "title": "Notes", "list": "notes"},
    {"id": "sec-phases", "title": "Phases", "list": "phases"},
    {"id": "sec-tasks", "title": "Tasks", "list": "tasks"},
    {"id": "sec-questions", "title": "Questions", "list": "questions"},
    {"id": "sec-followups", "title": "Follow ups", "list": "followups"},
]
OUTLINE_FNS = ["outlineOf", "itemState"]


def states(doc):
    tree = run_js(OUTLINE_FNS, f"outlineOf({json.dumps(doc)}, {json.dumps(HEADS)})")
    return {e["id"]: e.get("state") for s in tree for e in s["items"]}


class OutlineDotsByState(unittest.TestCase):
    def test_entries_with_a_done_state_carry_open_done_or_out(self):
        self.assertEqual(
            states(DOC),
            {
                "item-sources-0": None,
                "item-notes-n1": None,
                "item-phases-p1": "open",
                "item-phases-p2": "done",
                "item-tasks-t1": "open",
                "item-tasks-t2": "done",
                "item-tasks-t3": "out",
                "item-questions-q1": "open",
                "item-questions-q2": "done",
                "item-questions-q3": "open",
                "item-questions-q4": "out",
            },
        )

    def test_a_state_change_changes_the_outline_signature_so_the_dots_update_live(self):
        flipped = json.loads(json.dumps(DOC))
        flipped["phases"][0]["done"] = True
        self.assertEqual(states(flipped)["item-phases-p1"], "done")
        self.assertIn("JSON.stringify(tree)", function_source("renderOutline"))

    def test_the_outline_link_carries_its_state_as_a_class(self):
        expr = """(() => {
  const group = outlineGroup({ id: "sec-tasks", title: "Tasks", items: [
    { id: "item-tasks-a", title: "a", state: "done" }, { id: "item-tasks-b", title: "b" }] });
  const items = group.kids[0].kids[1].kids.map((li) => li.kids[0].attrs.class || "");
  return { items, head: group.kids[0].kids[0].kids[0].attrs.class || "" };
})()"""
        self.assertEqual(
            run_js(["outlineLink", "outlineGroup"], expr, STUB_H + STUB_FOLD + STUB_PAGES),
            {"items": ["st-done", ""], "head": ""},
        )

    def test_each_state_paints_its_palette_token_above_the_active_highlight(self):
        for state, token in (("open", "--signal"), ("done", "--positive"), ("out", "--warn")):
            rule = css_rule(f".outline .ol-items a.st-{state}::before, .outline .ol-items a.st-{state}.on::before")
            self.assertIsNotNone(rule, state)
            self.assertIn(f"color: var({token})", rule, state)
        self.assertIn("color: var(--accent)", css_rule(".outline a.on::before"))


class CheckedBoxesGreen(unittest.TestCase):
    def test_a_checked_box_is_filled_positive_with_a_contrasting_check_mark(self):
        checked = css_rule("input[type=checkbox]:checked")
        self.assertIn("background: var(--positive)", checked)
        self.assertIn("color: var(--canvas)", checked)
        mark = css_rule("input[type=checkbox]:checked::after")
        self.assertIn("border: 2px solid currentColor", mark)
        unchecked = css_rule("input[type=checkbox]")
        self.assertIn("background: var(--surface-2)", unchecked)


if __name__ == "__main__":
    unittest.main()
