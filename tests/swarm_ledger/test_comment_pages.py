import json
import subprocess
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "static" / "js"

HARNESS = """
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.on = {}; this.textContent = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { this.on[ev] = fn; }
  append(...kids) { this.kids.push(...kids); }
}
const document = { createElement: (tag) => new El(tag) };
%(pages)s
const ids = (n) => Array.from({ length: n }, (_, i) => `c${i + 1}`);
const out = {};
%(body)s
process.stdout.write(JSON.stringify(out));
"""


def pages_module():
    dom = (MODULES / "dom.js").read_text(encoding="utf-8")
    h = "function h(" + dom.split("export function h(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    pages = (MODULES / "pages.js").read_text(encoding="utf-8")
    body = "\n".join(line.removeprefix("export ") for line in pages.splitlines() if not line.startswith("import "))
    return h + body


def run(body):
    script = HARNESS % {"pages": pages_module(), "body": body}
    done = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def test_comment_page_is_eight_first_then_ten_per_click():
    assert run("out.page = COMMENT_PAGE;")["page"] == {"first": 8, "more": 10, "below": True}


def test_a_comment_list_shows_its_newest_eight():
    out = run('out.shown = lastPage("t1/comments", ids(25), (c) => c, COMMENT_PAGE);')
    assert out["shown"] == [f"c{i}" for i in range(18, 26)]


def test_show_more_reveals_ten_older_per_click_until_none_remain():
    out = run(
        """
const key = "t1/comments", list = ids(25), clicks = [];
const again = () => clicks.push(lastPage(key, list, (c) => c, COMMENT_PAGE));
for (let i = 0; i < 4; i++) {
  const more = moreButton(key, list.length, "older comments", again, "div", COMMENT_PAGE);
  if (!more) { out.endedAfter = i; break; }
  out[`label${i}`] = more.kids[0].textContent;
  more.kids[0].on.click();
}
out.counts = clicks.map((c) => c.length);
out.firstShown = clicks.map((c) => c[0]);
"""
    )
    assert out["counts"] == [18, 25]
    assert out["firstShown"] == ["c8", "c1"]
    assert out["label0"] == "Show 10 older comments · 17 left"
    assert out["label1"] == "Show 7 older comments · 7 left"
    assert out["endedAfter"] == 2


def test_a_short_list_has_no_show_more():
    out = run('out.more = moreButton("t2/comments", 8, "older comments", () => {}, "div", COMMENT_PAGE);')
    assert out["more"] is None


def test_the_count_is_per_list_and_survives_new_comments():
    out = run(
        """
const list = ids(20);
moreButton("a/comments", list.length, "older comments", () => {}, "div", COMMENT_PAGE).kids[0].on.click();
list.push("c21");
out.a = lastPage("a/comments", list, (c) => c, COMMENT_PAGE).length;
out.b = lastPage("b/comments", list, (c) => c, COMMENT_PAGE).length;
"""
    )
    assert out == {"a": 18, "b": 8}


@pytest.mark.parametrize(("target", "expected"), [("c18", 8), ("c17", 18), ("c5", 28), ("c1", 28)])
def test_a_jump_to_an_older_comment_opens_just_enough_pages(target, expected):
    out = run(f'wanted.id = "{target}"; out.n = lastPage("j/comments", ids(25), (c) => c, COMMENT_PAGE).length;')
    assert out["n"] == min(expected, 25)


def test_other_lists_keep_pages_of_fifty():
    out = run(
        """
out.first = lastPage("chat", ids(120), (c) => c).length;
const more = moreButton("chat", 120, "older messages", () => {}, "div");
out.label = more.kids[0].textContent;
more.kids[0].on.click();
out.after = lastPage("chat", ids(120), (c) => c).length;
"""
    )
    assert out == {"first": 50, "label": "Show 50 older messages · 70 left", "after": 100}


THREAD = """
const parts = (page) => threadPage("%s", ids(25), { idOf: (c) => c, label: "older comments", again: () => {}, make: (c) => c }, page);
out.comments = parts(COMMENT_PAGE).map((p) => (typeof p === "string" ? p : `more: ${p.kids[0].textContent}`));
out.notes = parts(undefined).map((p) => (typeof p === "string" ? p : "more"));
"""


def test_a_comment_thread_puts_show_more_under_its_newest_eight():
    out = run(THREAD % "x/comments")
    assert out["comments"] == [f"c{i}" for i in range(18, 26)] + ["more: Show 10 older comments · 17 left"]


def test_other_threads_keep_their_older_control_above_a_full_page():
    out = run(THREAD.replace("ids(25)", "ids(60)") % "notes")
    assert out["notes"] == ["more"] + [f"c{i}" for i in range(11, 61)]


def test_comment_dropdowns_pass_the_comment_page():
    source = (MODULES / "threads.js").read_text(encoding="utf-8")
    assert 'threadView(`${key}/comments`, list, "thread flat", COMMENT_PAGE)' in source
