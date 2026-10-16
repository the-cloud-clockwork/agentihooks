import base64
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
globalThis.document = { createElement: (tag) => new El(tag) };
const pages = await import("data:text/javascript;base64," + %(pages)s);
const { COMMENT_PAGE, lastPage, moreButton, wanted } = pages;
const ids = (n) => Array.from({ length: n }, (_, i) => `c${i + 1}`);
const out = {};
%(body)s
process.stdout.write(JSON.stringify(out));
"""


def pages_module():
    dom = (MODULES / "dom.js").read_text(encoding="utf-8")
    h = "function h(" + dom.split("export function h(", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    pages = (MODULES / "pages.js").read_text(encoding="utf-8")
    body = "\n".join(line for line in pages.splitlines() if not line.startswith("import "))
    return base64.b64encode((h + body).encode()).decode()


def run(body):
    script = HARNESS % {"pages": json.dumps(pages_module()), "body": body}
    done = subprocess.run(["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def test_comment_page_is_eight_first_then_ten_per_click():
    assert run("out.page = COMMENT_PAGE;")["page"] == {"first": 8, "more": 10}


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


def test_comment_threads_use_the_comment_page_with_show_more_under_the_list():
    source = (MODULES / "threads.js").read_text(encoding="utf-8")
    view = source.split("export function threadView(", 1)[1].split("\n}\n", 1)[0]
    assert 'path.endsWith("/comments") ? COMMENT_PAGE : FULL_PAGE' in view
    assert "comments ? [...entries, older] : [older, ...entries]" in view
