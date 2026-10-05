import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
VOID = {"meta", "input", "br", "img", "hr", "link", "path"}
SECTIONS = {
    "Overview": True,
    "Original sources": False,
    "Priorities": False,
    "Plan phases": True,
    "Swarm tasks": False,
    "Open questions": True,
    "Operator notes": True,
    "Follow-ups or blockers": True,
    "Stats": True,
    "Swarm": True,
    "Swarm health": True,
    "Chat": True,
    "Notifications": True,
}


def page():
    return TEMPLATE.read_text(encoding="utf-8")


def function_source(name):
    return f"function {name}(" + page().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.kids, self.text = tag, dict(attrs), parent, [], ""


class Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = self.cur = Node("root", {}, None)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.kids.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.text += data


def walk(node, tag):
    for kid in node.kids:
        if kid.tag == tag:
            yield kid
        yield from walk(kid, tag)


def sections():
    tree = Tree()
    tree.feed(page().split("<body>", 1)[1].split("<script", 1)[0])
    return list(walk(tree.root, "section"))


class EverySectionFolds(unittest.TestCase):
    def test_every_section_the_page_renders_has_a_fold_header(self):
        found = {}
        for section in sections():
            label = section.attrs.get("id") or section.attrs.get("aria-label") or "a section"
            self.assertTrue(section.kids, label)
            box = section.kids[0]
            self.assertEqual(box.tag, "details", f"{label} has a fixed header")
            self.assertIn("fold", box.attrs.get("class", "").split(), label)
            self.assertTrue(box.attrs.get("id"), label)
            self.assertEqual(box.kids[0].tag, "summary", label)
            summary = box.kids[0]
            found[summary.text.strip() or summary.kids[0].text.strip()] = "open" in box.attrs
        self.assertEqual(found, SECTIONS)

    def test_a_closed_dropdown_inside_an_open_section_shows_the_closed_arrow(self):
        self.assertIn("details[open] > summary::before { transform: rotate(90deg); }", page())
        self.assertNotIn("details[open] summary::before", page())

    def test_no_section_is_built_outside_the_page_markup(self):
        self.assertNotIn('h("section"', page())
        self.assertNotIn('createElement("section"', page())

    def test_the_one_builder_wires_every_fold_before_the_first_render(self):
        start = function_source("start")
        wire = start.index('for (const box of document.querySelectorAll("details.fold")) collapsible(box);')
        self.assertLess(wire, start.index("render();"))
        self.assertIn("collapsible(box);", function_source("outlineGroup"))

    def test_sections_with_comment_dropdowns_carry_show_and_hide_all_comments(self):
        with_comments = {"sec-phases", "sec-tasks", "sec-questions", "sec-followups"}
        for section in sections():
            buttons = [k.attrs.get("data-comments") for k in walk(section.kids[0].kids[0], "button")]
            want = ["show", "hide"] if section.attrs.get("id") in with_comments else []
            self.assertEqual([b for b in buttons if b], want, section.attrs.get("id"))
        start = function_source("start")
        self.assertIn('document.querySelectorAll("button[data-comments]")', start)
        self.assertIn('setAllComments(btn.closest("section"), btn.dataset.comments === "show")', start)


def run_builder(saved, script):
    harness = f"""
const store = {{ "plan-ledger:demo:fold": {json.dumps(json.dumps(saved))} }};
const localStorage = {{ getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => {{ store[k] = String(v); }} }};
const FOLD_KEY = "plan-ledger:demo:fold";
function fakeBox(id, open) {{
  const on = {{}};
  const summary = {{ on: {{}}, addEventListener(ev, fn) {{ this.on[ev] = fn; }} }};
  return {{ id, open, on, summary, querySelector: (sel) => (sel === ":scope > summary" ? summary : null),
    addEventListener(ev, fn) {{ on[ev] = fn; }} }};
}}
function click(box, inButton) {{
  let prevented = false;
  box.summary.on.click({{ target: {{ closest: (sel) => (inButton && sel === "button, a" ? {{}} : null) }}, preventDefault: () => {{ prevented = true; }} }});
  return prevented;
}}
{function_source("foldSaved")}
{function_source("collapsible")}
process.stdout.write(JSON.stringify((() => {{ {script} }})()));
"""
    out = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


class Builder(unittest.TestCase):
    def test_a_saved_state_wins_over_the_default_and_an_unsaved_box_keeps_it(self):
        out = run_builder(
            {"a": False, "b": True},
            """const a = fakeBox("a", true), b = fakeBox("b", false), c = fakeBox("c", true), d = fakeBox("d", false);
for (const x of [a, b, c, d]) collapsible(x);
return [a.open, b.open, c.open, d.open];""",
        )
        self.assertEqual(out, [False, True, True, False])

    def test_every_toggle_is_remembered_for_this_viewer(self):
        out = run_builder(
            {"other": True},
            """const a = fakeBox("a", true);
collapsible(a);
a.open = false; a.on.toggle();
const closed = JSON.parse(store[FOLD_KEY]);
a.open = true; a.on.toggle();
return [closed, JSON.parse(store[FOLD_KEY])];""",
        )
        self.assertEqual(out, [{"other": True, "a": False}, {"other": True, "a": True}])

    def test_unreadable_storage_falls_back_to_the_page_defaults(self):
        out = run_builder(
            {},
            """store[FOLD_KEY] = "{broken";
const a = fakeBox("a", true);
collapsible(a);
return [a.open, foldSaved()];""",
        )
        self.assertEqual(out, [True, {}])

    def test_a_header_button_does_its_job_without_toggling_the_section(self):
        out = run_builder(
            {}, """const a = fakeBox("a", true); collapsible(a); return [click(a, true), click(a, false)];"""
        )
        self.assertEqual(out, [True, False])


class CommentsControl(unittest.TestCase):
    def run_js(self, script):
        harness = f"""
const openComments = new Set(["followups/f1"]);
const closedComments = new Set(["phases/p2"]);
function fakeSection(keys, foldOpen) {{
  const fold = {{ open: foldOpen }};
  const boxes = keys.map((key) => ({{ open: key === "phases/p1", dataset: {{ key }} }}));
  return {{ fold, boxes, querySelector: (sel) => (sel === "details.fold" ? fold : null),
    querySelectorAll: (sel) => (sel === "details[data-key]" ? boxes : []) }};
}}
{function_source("setAllComments")}
process.stdout.write(JSON.stringify((() => {{ {script} }})()));
"""
        return json.loads(subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True).stdout)

    def test_show_all_opens_only_its_own_sections_comments_and_the_section(self):
        out = self.run_js("""const phases = fakeSection(["phases/p1", "phases/p2"], false), follow = fakeSection(["followups/f1", "followups/f2"], true);
setAllComments(phases, true);
return [phases.fold.open, phases.boxes.map((b) => b.open), follow.boxes.map((b) => b.open), [...openComments].sort(), [...closedComments]];""")
        self.assertEqual(out, [True, [True, True], [False, False], ["followups/f1", "phases/p1", "phases/p2"], []])

    def test_hide_all_closes_only_its_own_sections_comments_and_survives_a_rerender(self):
        out = self.run_js("""const phases = fakeSection(["phases/p1", "phases/p2"], true), follow = fakeSection(["followups/f1"], true);
follow.boxes[0].open = true;
setAllComments(phases, false);
return [phases.fold.open, phases.boxes.map((b) => b.open), follow.boxes[0].open, [...openComments], [...closedComments].sort()];""")
        self.assertEqual(out, [True, [False, False], True, ["followups/f1"], ["phases/p1", "phases/p2"]])


class FakeDom:
    SCRIPT = """
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.on = {}; this.textContent = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { this.on[ev] = fn; }
  append(...kids) { this.kids.push(...kids); }
}
const document = { createElement: (tag) => new El(tag) };
const wired = [];
function collapsible(box) { wired.push(box.attrs.id); }
function shape(el) { return { tag: el.tag, attrs: el.attrs, text: el.textContent, kids: el.kids.map(shape) }; }
"""


class OutlineFolds(unittest.TestCase):
    DOC = {
        "sources": ["a.md", "b.md"],
        "priorities": [{"id": "pr1", "item": "followups/f1", "text": "Answer the port"}],
        "phases": [{"id": "p1", "title": "Build it"}],
        "tasks": [],
        "questions": [{"id": "q1", "text": "Which port?"}],
        "notes": [{"id": "n1", "text": "Keep it small"}, {"id": "n2", "text": "", "deleted": True}],
        "followups": [{"id": "f1", "text": "Rotate the key"}],
    }
    HEADS = [
        {"id": "sec-overview", "title": "Overview", "list": ""},
        {"id": "sec-sources", "title": "Original sources", "list": "sources"},
        {"id": "sec-priorities", "title": "Priorities", "list": "priorities"},
        {"id": "sec-phases", "title": "Plan phases", "list": "phases"},
        {"id": "sec-tasks", "title": "Swarm tasks", "list": "tasks"},
        {"id": "sec-questions", "title": "Open questions", "list": "questions"},
        {"id": "sec-notes", "title": "Operator notes", "list": "notes"},
        {"id": "sec-followups", "title": "Follow-ups or blockers", "list": "followups"},
    ]

    def build(self):
        script = (
            FakeDom.SCRIPT
            + "".join(function_source(n) + "\n" for n in ("h", "itemState", "outlineOf", "outlineGroup"))
            + f"const tree = outlineOf({json.dumps(self.DOC)}, {json.dumps(self.HEADS)});"
            + "process.stdout.write(JSON.stringify({ groups: tree.map((s) => shape(outlineGroup(s))), wired, tree }));"
        )
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_every_outline_category_lists_its_own_items(self):
        tree = {s["id"]: s["items"] for s in self.build()["tree"]}
        self.assertEqual(tree["sec-overview"], [])
        self.assertEqual(
            tree["sec-sources"],
            [{"id": "item-sources-0", "title": "a.md"}, {"id": "item-sources-1", "title": "b.md"}],
        )
        self.assertEqual(tree["sec-priorities"], [{"id": "item-priorities-pr1", "title": "Answer the port"}])
        self.assertEqual(tree["sec-questions"], [{"id": "item-questions-q1", "title": "Which port?", "state": "open"}])
        self.assertEqual(tree["sec-notes"], [{"id": "item-notes-n1", "title": "Keep it small"}])
        self.assertEqual(
            tree["sec-followups"], [{"id": "item-followups-f1", "title": "Rotate the key", "state": "open"}]
        )

    def test_every_outline_category_has_a_toggle_even_without_items(self):
        out = self.build()
        self.assertEqual(len(out["groups"]), len(self.HEADS))
        for group, head in zip(out["groups"], self.HEADS):
            self.assertEqual(group["tag"], "li")
            box = group["kids"][0]
            self.assertEqual(box["tag"], "details", head["title"])
            self.assertIn("fold", box["attrs"]["class"].split(), head["title"])
            self.assertIn("open", box["attrs"])
            summary, items = box["kids"]
            self.assertEqual(summary["tag"], "summary")
            self.assertEqual(summary["kids"][0]["attrs"]["data-target"], head["id"])
            self.assertEqual(items["attrs"]["class"], "ol-items")
        self.assertEqual(out["wired"], [f"ol-{h['id']}" for h in self.HEADS])

    def test_outline_has_expand_all_and_collapse_all_and_entries_still_jump(self):
        render = function_source("renderOutline")
        self.assertIn('foldAll("Expand all", true)', render)
        self.assertIn('foldAll("Collapse all", false)', render)
        self.assertIn('$("outline").querySelectorAll("details.fold")', function_source("foldAll"))
        self.assertIn("ev.preventDefault();", page().split('$("outline").addEventListener("click"', 1)[1])

    def test_every_listed_item_has_a_jump_target_on_the_page(self):
        self.assertIn("id: `item-sources-${n}`", function_source("render"))
        self.assertIn("id: `item-priorities-${p.id}`", function_source("renderPriorities"))
        self.assertIn('id: path === "notes" ? `item-notes-${entry.id}` : false', function_source("entryView"))
        self.assertIn("id: `item-${list}-${item.id}`", function_source("checkRow"))
        self.assertIn("id: `item-questions-${item.id}`", function_source("questionRow"))
        self.assertIn("id: `item-tasks-${item.id}`", function_source("taskRow"))


if __name__ == "__main__":
    unittest.main()
