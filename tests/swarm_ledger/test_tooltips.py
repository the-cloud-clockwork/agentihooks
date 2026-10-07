import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, serve_modules
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scripts" / "swarm_ledger"
sys.path.insert(0, str(LEDGER))
import ledger_core  # noqa: E402

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger import new_ledger  # noqa: E402

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
    html = (LEDGER / "template.html").read_text().replace("__LEDGER_DATA__", json.dumps(DOC))
    html = html.replace("__LEDGER_PALETTE__", (LEDGER / "palette.css").read_text())
    tips = LEDGER / "tooltips.js"
    return html.replace("/*__LEDGER_TOOLTIPS__*/", tips.read_text() if tips.exists() else "")


def home_html(view):
    row = {"slug": "s", "title": "T", "overview": "o", "size": "swarm", "open": 1, "done": 0, "updated_at": 0}
    with (
        patch.object(server, "ledger_summaries", return_value=[{**row, "closed_at": 0}, {**row, "closed_at": 5}]),
        patch.object(server, "bin_summaries", return_value=[{**row, "deleted_at": 0, "days_left": 3}]),
        patch.object(server, "swarm_state", return_value="running"),
        patch.object(server.ledger_bin, "entries", return_value=[{}]),
    ):
        return server.index_page(view, now=0)


def route(r, html):
    url = r.request.url
    if is_events(url):
        fulfill_events(r, swarm=STATUS)
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
def test_every_button_like_control_on_home_and_the_bin_has_a_tip_of_at_most_25_words(tab, view):
    page = tab(home_html(view))
    assert_every_control_has_a_short_tip(page, 2)


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
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", js)
    assert re.findall(r"var\(--([\w-]+)\)", js)
    assert "<script>/*__LEDGER_TOOLTIPS__*/</script>" in (LEDGER / "template.html").read_text()
    assert js in home_html("home")
    content = {"title": "T", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    rendered = new_ledger.render(new_ledger.build_doc(content), "tips", 8765)
    assert f"<script>{js}</script>" in rendered
    assert "__LEDGER_" not in rendered


def test_the_page_version_follows_the_tip_module(tmp_path):
    before = core.page_version()
    changed = tmp_path / "tooltips.js"
    changed.write_text(core.TOOLTIPS.read_text() + ";")
    with patch.object(core, "TOOLTIPS", changed):
        after = core.page_version()
    assert re.fullmatch(r"[0-9a-f]{12}", before)
    assert after != before


def test_upgrading_a_ledger_page_inlines_the_tip_module():
    content = {"title": "T", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = ledger_core.paths("tips-upgrade")
    ledger_core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        new_ledger.render(new_ledger.build_doc(content), "tips-upgrade", 8765).replace(core.TOOLTIPS.read_text(), "")
    )
    json_path.unlink(missing_ok=True)
    ledger_core.sync("tips-upgrade")
    new_ledger.upgrade_page("tips-upgrade")
    page = html_path.read_text()
    assert f"<script>{core.TOOLTIPS.read_text()}</script>" in page
    assert "__LEDGER_" not in page
