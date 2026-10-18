from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://ledger.test/priority-titles"
DOC = {
    "title": "Priority titles proof",
    "overview": "Find what a priority points at.",
    "phases": [{"id": "p7", "title": "Carry over the old alias", "description": "Work"}],
    "questions": [{"id": "q-x83kd", "text": "Which port should the demo use?"}],
    "followups": [{"id": "f-9zq1m", "text": "Retire the second browser"}],
    "tasks": [{"id": "pt1", "title": "Priorities show their item title", "lane": "eng", "state": "pr"}],
    "priorities": [
        {"id": "a1", "item": "tasks/pt1", "text": "Approve the merge"},
        {"id": "a2", "item": "phases/p7", "text": "Choose the first phase"},
        {"id": "a3", "item": "questions/q-x83kd", "text": "Answer the port question"},
        {"id": "a4", "item": "followups/f-9zq1m", "text": "Decide on the browser"},
        {"id": "a5", "item": "followups/f-gone1", "text": "Decide on the removed one"},
    ],
}
SWARM = {"config": {"state": "running"}, "tasks": {}, "agents": [], "findings": [], "spawns": {}}
ROWS = [
    ("a1", "pt1", "Priorities show their item title", "Approve the merge", "#item-tasks-pt1"),
    ("a2", "p7", "Carry over the old alias", "Choose the first phase", "#item-phases-p7"),
    ("a3", "question", "Which port should the demo use?", "Answer the port question", "#item-questions-q-x83kd"),
    ("a4", "follow up", "Retire the second browser", "Decide on the browser", "#item-followups-f-9zq1m"),
]


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = shell_html()

    def route(request):
        if "/api/swarm/" in request.request.url:
            request.fulfill(json=SWARM)
        elif request.request.url.startswith(URL):
            request.fulfill(body=html, content_type="text/html")
        else:
            request.abort()

    context.route("**/*", route)
    serve_modules(context, ledger_state(DOC))
    page = context.new_page()
    page.goto(URL + "#ledger")
    loaded(page)
    yield page
    context.close()


@pytest.mark.parametrize(("prio", "label", "title", "ask", "href"), ROWS)
def test_each_priority_leads_with_a_red_label_then_its_item_title_and_the_ask_below(tab, prio, label, title, ask, href):
    row = tab.locator(f"#item-priorities-{prio}")
    link = row.locator(".prio-link")
    assert link.text_content() == label
    assert link.get_attribute("href") == href
    assert row.locator(".prio-title").text_content() == title
    assert row.locator(".prio-text").text_content() == ask
    assert link.evaluate("el => getComputedStyle(el).color") == "rgb(239, 68, 68)"
    title_box, ask_box = (row.locator(s).bounding_box() for s in (".prio-title", ".prio-text"))
    assert ask_box["y"] >= title_box["y"] + title_box["height"] - 1


def test_no_priority_shows_the_internal_item_id_of_a_question_or_follow_up(tab):
    text = tab.locator("#priorities").inner_text()
    for raw in ("q-x83kd", "f-9zq1m", "f-gone1", "#"):
        assert raw not in text


def test_clicking_the_item_title_jumps_to_the_item(tab):
    tab.locator("#item-priorities-a4 .prio-title").click()
    target = tab.locator("#item-followups-f-9zq1m")
    target.wait_for(state="visible")
    assert "flash" in target.get_attribute("class")


def test_a_priority_whose_item_was_removed_says_so(tab):
    row = tab.locator("#item-priorities-a5")
    assert row.locator(".prio-link").text_content() == "follow up"
    assert row.locator(".prio-title").text_content() == "This follow up was removed"
    assert row.locator(".prio-text").text_content() == "Decide on the removed one"
