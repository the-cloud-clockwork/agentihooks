import re
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, serve_modules, served, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scripts" / "swarm_ledger"
sys.path.insert(0, str(LEDGER))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402

URL = "http://ledger.test/tips"
CONTROLS = 'button, [role="tab"], [role="button"], [role="switch"], a.sync, a.fab'
NOT_CONTROLS = "input, textarea, select, #overview a, .entry-body a, h1, .prio-text"
IMAGE = {"id": f"{1:064x}.png", "type": "image/png", "size": 10, "name": "shot.png"}
COMMENT = {"id": "c1", "by": "operator", "at": 1_000, "text": "See https://example.com", "attachments": [IMAGE]}
DOC = {
    "title": "Tooltips",
    "overview": "Read https://example.com first.",
    "sources": [{"label": "plan", "path": "plan.md"}],
    "notes": [{"id": "n1", "by": "operator", "at": 1_000, "text": "Note", "comments": [COMMENT]}],
    "questions": [{"id": "q1", "title": "Which?", "comments": [COMMENT]}],
    "phases": [
        {"id": "p1", "title": "Built", "description": "d", "done": True, "comments": [COMMENT]},
        {"id": "p2", "title": "Sliced", "description": "d", "planning": "auto", "comments": []},
    ],
    "tasks": [
        {"id": "t1", "title": "Plan p2", "phase": "p2", "kind": "plan", "state": "done", "comments": []},
        {"id": "t2", "title": "Build", "phase": "p2", "state": "claimed", "comments": []},
    ],
    "followups": [{"id": "f1", "title": "Later", "comments": []}],
    "priorities": [{"id": "pr1", "item": "phases/p2", "text": "Review the plan"}],
    "notifications": [{"id": "x1", "item": "phases/p1", "label": "Comment", "by": "boss", "at": 1_000, "text": "Hi"}],
    "chat": [{"id": "m1", "by": "operator", "at": 1_000, "text": "hello"}],
    "artifacts": [{"id": "a1", "title": "Shot", "by": "eng", "task": "t2", "at": 1_000, "file": IMAGE}],
    "artifact_trash": [{"id": "a2", "title": "Old", "by": "eng", "task": "t2", "at": 1_000, "deleted_at": 1_000}],
}
STATUS = {
    "config": {"slug": "tips", "state": "running", "max_eng": 2, "max_ci": 1, "max_plan": 1, "autonomy": "delegate"},
    "tasks": {"open": 1, "claimed": 1, "pr": 0, "blocked": 0, "done": 1},
    "agents": [
        {"name": "master-1", "lane": "master", "harness": "claude", "status": "working", "inbox": []},
        {"name": "eng-1", "lane": "eng", "harness": "claude", "task": "t2", "status": "idle", "inbox": []},
    ],
    "findings": [{"id": "idle/eng-1", "kind": "idle", "subject": "eng-1", "evidence": ["3 ticks"], "threshold": "3"}],
    "handoffs": [{"seat": "eng-1@tips", "at": 1_000, "reason": "limit", "awaiting": "eng-1"}],
    "doctor": {"slug": "", "state": "not running", "last_check": 0, "findings": 0},
    "quota": {"cap": 3, "rows": []},
}


def ledger_html():
    return shell_html()


ROW_TITLE = "Swarm design system & home <rows>"
ROW_OVERVIEW = 'Borrow what OpenRig does that we lack, while keeping the ledger "quota" routing.'
ROW_AT = 90_000_000
ROW = {"slug": "s", "title": ROW_TITLE, "overview": ROW_OVERVIEW, "size": "swarm", "open": 1, "done": 0}
COLLECTIONS = {
    "/api/v1/ledgers?": [{**ROW, "swarm": "running", "updated_at": ROW_AT, "closed_at": 0}],
    "/api/v1/bin?": [{**ROW, "deleted_at": 0, "days_left": 3}],
}


def home_html(view):
    return server.index_page(view)


def route(r, html):
    url = r.request.url
    rows = next((rows for path, rows in COLLECTIONS.items() if path in url), None)
    if is_events(url):
        fulfill_events(r, ledger=ledger_state(DOC), swarm=STATUS)
    elif rows is not None:
        r.fulfill(json={"data": rows, "next_cursor": None})
    elif "/api/swarm/" in url:
        r.fulfill(json=STATUS)
    elif "/api/" in url:
        r.fulfill(json={})
    elif "/media/" in url or "/artifacts/" in url:
        r.fulfill(body="# Shot", content_type="text/markdown")
    else:
        r.fulfill(body=html, content_type="text/html")


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    errors = []

    def open_page(html, fragment=""):
        context.route("**/*", lambda r: route(r, html))
        serve_modules(context)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL + fragment)
        page.set_default_timeout(3000)
        return page

    yield open_page
    context.close()
    assert errors == []


def walk(page):
    return page.evaluate(
        """([controls, others]) => ({
          missing: [...document.querySelectorAll(controls)].map((el) => [el.outerHTML.slice(0, 160), window.ledgerTip ? window.ledgerTip(el) : null]),
          extra: [...document.querySelectorAll(others)].map((el) => [el.outerHTML.slice(0, 160), window.ledgerTip ? window.ledgerTip(el) : null]),
        })""",
        [CONTROLS, NOT_CONTROLS],
    )


def assert_every_control_has_a_short_tip(page, least):
    found = walk(page)
    bare = [html for html, tip in found["missing"] if not tip]
    long = [(html, tip) for html, tip in found["missing"] if tip and len(tip.split()) > 25]
    assert len(found["missing"]) >= least
    assert bare == []
    assert long == []
    assert [html for html, tip in found["extra"] if tip] == []


def test_every_button_like_control_on_the_ledger_page_has_a_tip_of_at_most_25_words(tab):
    page = tab(ledger_html(), "#swarm")
    for row in ("#swarm-agents [data-terminate]", "[data-restore-choice]", ".phase-review button"):
        page.locator(row).first.wait_for(state="attached")
    page.locator("#chat-fab").click()
    page.locator("#art-fab").click()
    page.locator(".art-title").first.click()
    assert_every_control_has_a_short_tip(page, 80)
    page.keyboard.press("Escape")
    page.locator("#tab-ledger").click()
    page.locator("#comments-all").click()
    page.locator(".thumb").first.click()
    page.locator('[aria-label="Next image"]').wait_for(state="attached")
    assert_every_control_has_a_short_tip(page, 80)


@pytest.mark.parametrize("view", ["home", "bin"])
def test_every_button_like_control_on_home_and_the_bin_has_a_tip_of_at_most_25_words(browser, view):
    row = {"slug": "s", "title": "T", "overview": "o", "size": "swarm", "open": 1, "done": 0, "updated_at": 0}
    with (
        patch.object(server, "ledger_summaries", return_value=[{**row, "closed_at": 0}, {**row, "closed_at": 5}]),
        patch.object(server, "bin_summaries", return_value=[{**row, "deleted_at": 0, "days_left": 3}]),
        patch.object(server, "swarm_state", return_value="running"),
        patch.object(server.ledger_bin, "entries", return_value={}),
    ):
        with served(server) as base:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            errors = []
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.set_default_timeout(3000)
            page.goto(base + ("/?view=bin" if view == "bin" else "/"))
            page.wait_for_function("() => document.getElementById('rows').children.length > 0")
            assert_every_control_has_a_short_tip(page, 2)
            context.close()
    assert errors == []


def tip_shown(page):
    return page.evaluate(
        """() => { const t = document.querySelector(".ledger-tip");
          return t && t.matches(":popover-open") ? t.textContent : null; }"""
    )


def hover_stop_now(page):
    page.clock.install(time=0)
    page.locator("#tab-swarm").click()
    page.clock.pause_at(60_000)
    button = page.locator('[data-swarm="stop_now"]')
    button.hover()
    return button


def test_the_tip_appears_exactly_one_second_after_the_pointer_rests(tab):
    page = tab(ledger_html())
    hover_stop_now(page)
    page.clock.run_for(999)
    assert tip_shown(page) is None
    page.clock.run_for(1)
    assert tip_shown(page) == page.evaluate("""ledgerTip(document.querySelector('[data-swarm="stop_now"]'))""")
    assert page.locator('[data-swarm="stop_now"]').get_attribute("title") is None


ROW_TIPS = {
    "a.title": ROW_TITLE,
    "span.ov": ROW_OVERVIEW,
    "time.when": time.strftime("%Y-%m-%d %H:%M", time.localtime(ROW_AT / 1000)),
}


@pytest.mark.parametrize("cell", ROW_TIPS)
def test_a_home_row_shows_its_full_text_in_the_tip_exactly_one_second_after_the_pointer_rests(tab, cell):
    page = tab(home_html("home"))
    page.clock.install(time=0)
    page.clock.pause_at(60_000)
    page.locator(f"li.row {cell}").first.hover()
    page.clock.run_for(999)
    assert tip_shown(page) is None
    page.clock.run_for(1)
    assert tip_shown(page) == ROW_TIPS[cell]
    page.mouse.move(2, 890)
    assert tip_shown(page) is None


@pytest.mark.parametrize("leave", ["click", "scroll"])
def test_a_home_row_tip_hides_on_click_or_scroll(tab, leave):
    page = tab(home_html("home"))
    page.clock.install(time=0)
    page.clock.pause_at(60_000)
    page.locator("li.row span.ov").first.hover()
    page.clock.run_for(1000)
    assert tip_shown(page) == ROW_OVERVIEW
    if leave == "click":
        page.evaluate(
            """document.querySelector("li.row span.ov").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }))"""
        )
    else:
        page.evaluate("window.dispatchEvent(new Event('scroll'))")
    assert tip_shown(page) is None


def test_an_empty_row_tip_leaves_the_enclosing_control_its_own_tip(tab):
    page = tab(home_html("home"))
    page.locator("li.row button.fold").wait_for()
    found = page.evaluate(
        """() => { const fold = document.querySelector("li.row button.fold");
          const empty = document.createElement("span");
          empty.dataset.tip = "";
          fold.append(empty);
          return [ledgerTip(empty), ledgerTip(fold)]; }"""
    )
    assert found == ["Show this ledger's full title and overview, or fold it back to one line."] * 2


@pytest.mark.parametrize("view", ["home", "bin"])
def test_ledger_rows_carry_no_native_hover_text(tab, view):
    page = tab(home_html(view), "?view=bin" if view == "bin" else "")
    cells = page.locator("li.row :is(.title, .ov, .when)")
    cells.first.wait_for()
    found = cells.evaluate_all(
        "(els) => els.map((el) => [el.className, el.getAttribute('title'), el.dataset.tip ?? null])"
    )
    assert [title for _, title, _ in found] == [None] * len(found)
    assert ["title", None, ROW_TITLE] in found
    assert ["ov", None, ROW_OVERVIEW] in found


ROW_BUTTONS = {
    "home": (
        ".act.del",
        f"Move {ROW_TITLE} to the bin",
        "Move this ledger to the bin. Its swarm stops; the bin keeps it thirty days.",
    ),
    "bin": (".act.restore", f"Restore {ROW_TITLE} to HOME", "Restore this ledger from the bin to HOME."),
}


@pytest.mark.parametrize("view", ROW_BUTTONS)
def test_ledger_row_buttons_carry_no_native_hover_text_and_keep_their_label(tab, view):
    page = tab(home_html(view), "?view=bin" if view == "bin" else "")
    page.locator("li.row .act").first.wait_for()
    buttons = page.locator("li.row .act, a.fab")
    assert buttons.count() == 2
    found = buttons.evaluate_all("(els) => els.map((el) => [el.getAttribute('title'), el.getAttribute('aria-label')])")
    assert [title for title, _ in found] == [None] * len(found)
    selector, label, _ = ROW_BUTTONS[view]
    assert page.locator(f"li.row {selector}").first.get_attribute("aria-label") == label


@pytest.mark.parametrize("view", ROW_BUTTONS)
def test_a_ledger_row_button_shows_the_ledger_tip_exactly_one_second_after_the_pointer_rests(tab, view):
    selector, _, tip = ROW_BUTTONS[view]
    page = tab(home_html(view), "?view=bin" if view == "bin" else "")
    page.clock.install(time=0)
    page.clock.pause_at(60_000)
    page.locator(f"li.row {selector}").first.hover()
    page.clock.run_for(999)
    assert tip_shown(page) is None
    page.clock.run_for(1)
    assert tip_shown(page) == tip
    page.mouse.move(2, 890)
    assert tip_shown(page) is None


@pytest.mark.parametrize("leave", ["pointer", "click", "scroll"])
def test_the_tip_hides_on_pointer_leave_click_or_scroll(tab, leave):
    page = tab(ledger_html())
    button = hover_stop_now(page)
    page.clock.run_for(1000)
    assert tip_shown(page)
    if leave == "pointer":
        page.mouse.move(2, 890)
    elif leave == "click":
        page.evaluate(
            """document.querySelector('[data-swarm="stop_now"]').dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }))"""
        )
    else:
        page.evaluate("window.dispatchEvent(new Event('scroll'))")
    assert tip_shown(page) is None
    page.clock.run_for(3000)
    assert tip_shown(page) is None
    assert button.is_visible()


def test_the_tip_module_holds_no_colour_and_is_served_with_both_pages():
    js = (LEDGER / "tooltips.js").read_text()
    css = (LEDGER / "static" / "css" / "tooltips.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", js)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css)
    assert re.findall(r"var\(--([\w-]+)\)", css)
    tag = f'<script src="/static/{core.page_version()}/tooltips.js"></script>'
    assert tag in home_html("home")
    content = {
        "title": "T",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "p", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    server.repository.create("tips", content)
    served_page = server.page_for("tips")
    assert tag in served_page
    assert "__LEDGER_" not in served_page


def test_the_page_version_follows_the_tip_module(tmp_path):
    before = core.page_version()
    changed = tmp_path / "tooltips.js"
    changed.write_text(core.TOOLTIPS.read_text() + ";")
    with patch.object(core, "TOOLTIPS", changed):
        after = core.page_version()
    assert re.fullmatch(r"[0-9a-f]{12}", before)
    assert after != before
