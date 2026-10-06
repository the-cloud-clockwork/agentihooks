import json
import re
from pathlib import Path

import pytest

from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://ledger.test/task-ids"
DOC = {
    "title": "Task id proof",
    "overview": "Find a task by its id.",
    "phases": [{"id": f"p{i}", "title": f"Phase {i}", "description": "Work"} for i in range(25)],
    "priorities": [{"id": "ask", "item": "tasks/rb3c", "text": "Approve the merge"}],
    "tasks": [
        {"id": "one", "title": "Build the first feature", "lane": "eng", "state": "claimed"},
        {"id": "rb3c", "title": "Stop masters editing code", "lane": "eng", "state": "done", "done": True},
    ],
    "chat": [{"id": "c1", "by": "master", "text": "rb3c merged, one is next", "at": 1}],
}
SWARM = {"config": {"state": "running"}, "tasks": {}, "agents": [], "findings": [], "spawns": {}}


def page_source():
    return (ROOT / "scripts/swarm_ledger/template.html").read_text()


def css_rule(selector):
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", page_source())
    return match and match.group(1)


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = page_source().replace("__LEDGER_DATA__", json.dumps(DOC))
    html = html.replace("__LEDGER_PALETTE__", (ROOT / "scripts/swarm_ledger/palette.css").read_text())

    def route(request):
        if "/api/swarm/" in request.request.url:
            request.fulfill(json=SWARM)
        elif request.request.url.startswith(URL):
            request.fulfill(body=html, content_type="text/html")
        else:
            request.abort()

    context.route("**/*", route)
    page = context.new_page()
    page.goto(URL + "#ledger")
    yield page
    context.close()


def test_every_task_card_leads_its_title_with_its_id_as_a_link_to_itself(tab):
    for task in DOC["tasks"]:
        title = tab.locator(f"#item-tasks-{task['id']} .item-title")
        label = title.locator(".task-id")
        assert label.count() == 1
        assert label.text_content() == task["id"]
        assert label.get_attribute("href") == f"#item-tasks-{task['id']}"
        assert title.evaluate("el => el.firstElementChild.className") == "task-id"
        assert task["title"] in title.text_content()


def test_the_id_label_is_bare_flat_red_text_in_the_priorities_red(tab):
    tab.locator("#tasks-box > summary").click()
    label = tab.locator("#item-tasks-rb3c .task-id")
    prio = tab.locator("#priorities .prio-link")
    style = (
        "el => { const s = getComputedStyle(el); return [s.color, s.textShadow, s.backgroundColor, s.borderTopStyle]; }"
    )
    color, shadow, background, border = label.evaluate(style)
    assert color == prio.evaluate("el => getComputedStyle(el).color")
    assert color == "rgb(239, 68, 68)"
    assert shadow == "none"
    assert background == "rgba(0, 0, 0, 0)"
    assert border == "none"
    rule = css_rule(".task-id")
    assert "color: var(--destructive)" in rule
    assert "text-shadow" not in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(background|border):", rule)
    assert "color: var(--destructive)" in css_rule(".prio-link")


def test_a_focused_id_label_keeps_a_visible_focus_ring(tab):
    tab.locator("#priorities .prio-link").click()
    label = tab.locator("#item-tasks-rb3c .task-id")
    label.wait_for(state="visible")
    label.focus()
    tab.keyboard.press("Tab")
    tab.keyboard.press("Shift+Tab")
    assert label.evaluate("el => el.matches(':focus-visible')")
    assert label.evaluate("el => getComputedStyle(el).outlineStyle") != "none"


def test_a_priorities_link_opens_the_folded_tasks_and_scrolls_to_the_card(tab):
    assert not tab.locator("#item-tasks-rb3c").is_visible()
    tab.locator("#priorities .prio-link").click()
    card = tab.locator("#item-tasks-rb3c")
    card.wait_for(state="visible")
    tab.wait_for_function(
        "() => { const r = document.getElementById('item-tasks-rb3c').getBoundingClientRect();"
        " return r.top >= 0 && r.bottom <= innerHeight; }"
    )


def test_a_task_id_in_chat_links_to_its_card(tab):
    links = tab.locator("#chat-log a.task-id")
    assert links.evaluate_all("els => els.map(a => [a.textContent, a.getAttribute('href')])") == [
        ["rb3c", "#item-tasks-rb3c"],
        ["one", "#item-tasks-one"],
    ]
    assert "merged, " in tab.locator("#chat-log .entry-body").text_content()
    assert not tab.locator("#item-tasks-rb3c").is_visible()
    tab.locator("#chat-fab").click()
    links.first.click()
    tab.locator("#item-tasks-rb3c").wait_for(state="visible")
    tab.wait_for_function(
        "() => { const r = document.getElementById('item-tasks-rb3c').getBoundingClientRect();"
        " return r.top >= 0 && r.bottom <= innerHeight; }"
    )


def test_the_outline_lists_each_task_by_id_and_title(tab):
    links = tab.locator("#ol-sec-tasks .ol-items a")
    assert links.evaluate_all("els => els.map(a => [a.textContent, a.dataset.target])") == [
        ["one Build the first feature", "item-tasks-one"],
        ["rb3c Stop masters editing code", "item-tasks-rb3c"],
    ]
