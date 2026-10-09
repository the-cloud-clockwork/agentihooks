from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://ledger.test/tabs"
DOC = {
    "title": "Tab proof",
    "overview": "Read and decide, then steer the swarm.",
    "phases": [{"id": f"p{i}", "title": f"Phase {i}", "description": "Work"} for i in range(25)],
    "priorities": [{"id": "urgent", "item": "phases/p0", "text": "Choose the first phase"}],
    "tasks": [{"id": "one", "title": "Build the first feature", "state": "claimed"}],
}
SWARM = {
    "config": {"state": "running", "max_eng": 3, "max_ci": 1},
    "tasks": {"claimed": 1},
    "agents": [{"name": "master", "lane": "master"}, {"name": "engineer", "lane": "eng", "task": "one"}],
    "findings": [],
    "spawns": {"codex": 6, "claude": 13},
}


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = shell_html()

    def route(request):
        if is_events(request.request.url):
            fulfill_events(request, ledger_state(DOC), SWARM)
        elif "/api/swarm/" in request.request.url:
            request.fulfill(json=SWARM)
        elif request.request.url.startswith(URL):
            request.fulfill(body=html, content_type="text/html")
        else:
            request.abort()

    context.route("**/*", route)
    serve_modules(context)
    page = context.new_page()
    page.on("pageerror", lambda error: print(str(error)))
    page.goto(URL)
    loaded(page)
    yield page
    context.close()


def test_tabs_keep_the_shell_fixed_and_scroll_only_the_active_body(tab):
    assert tab.get_by_role("tab", name="Ledger").count() == 1
    assert tab.get_by_role("tab", name="Swarm").count() == 1
    tab.locator("#main-content").evaluate("el => el.scrollTop = 600")
    assert tab.locator("header").bounding_box()["y"] == 0
    assert tab.evaluate("window.scrollY") == 0
    assert tab.locator("#main-content").evaluate("el => el.scrollTop") == 600
    assert tab.evaluate("""() => [...document.querySelectorAll('body *')].filter(el => {
        const style = getComputedStyle(el);
        return el.getClientRects().length && ['auto', 'scroll'].includes(style.overflowY)
          && el.scrollHeight > el.clientHeight;
    }).map(el => el.id)""") == ["outline", "main-content"]


def test_tab_choice_hash_keyboard_and_scroll_positions_survive_switches(tab):
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm").is_visible()
    assert not tab.locator("#ledger").is_visible()
    assert tab.url.endswith("#swarm")
    tab.reload()
    loaded(tab)
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL)
    loaded(tab)
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL + "#ledger")
    loaded(tab)
    assert tab.locator("#ledger").is_visible()
    tab.get_by_role("tab", name="Ledger").focus()
    tab.locator("#main-content").evaluate("el => el.scrollTop = 600")
    tab.keyboard.press("ArrowRight")
    assert tab.locator("#swarm").is_visible()
    tab.keyboard.press("Home")
    assert tab.locator("#ledger").is_visible()
    assert tab.locator("#main-content").evaluate("el => el.scrollTop") == 600
    tab.keyboard.press("End")
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL + "#item-phases-p20")
    loaded(tab)
    assert tab.locator("#ledger").is_visible()
    assert tab.locator("#item-phases-p20").is_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_swarm_contains_the_operational_blocks_and_nothing_overflows(tab, width):
    tab.set_viewport_size({"width": width, "height": 844})
    tab.get_by_role("tab", name="Swarm").click()
    for name in ["Capacity", "Agents", "Overlays", "Quota", "Doctor", "Health", "Handoff outcomes"]:
        assert tab.locator("#swarm").get_by_text(name, exact=True).count() == 1, name
    for gone in ["Needs you", "Crew history", "Last restore", "Tasks"]:
        assert tab.locator("#swarm").get_by_text(gone, exact=True).count() == 0, gone
    assert tab.locator("#swarm #cap-eng").input_value() == "3"
    assert tab.locator("#swarm-alert").is_hidden()
    assert tab.get_by_text("Needs you", exact=True).count() == 0
    assert tab.locator("#needs-you-box, #needs-you").count() == 0
    tab.get_by_role("tab", name="Ledger").click()
    assert "Choose the first phase" in tab.locator("#priorities").inner_text()
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("body").evaluate("el => el.scrollWidth") == width, tab.evaluate(
        "() => [...document.querySelectorAll('body *')].filter(el => el.getClientRects().length && el.getBoundingClientRect().right > innerWidth).map(el => [el.id, el.className, el.getBoundingClientRect().right])"
    )
    if width == 390:
        assert tab.locator("#chat-fab").evaluate("el => !el.closest('#icon-strip')")
        assert tab.locator("#bell").evaluate("el => !!el.closest('#icon-strip')")


def test_an_agent_row_links_its_task_id_to_the_task_in_the_ledger(tab):
    tab.get_by_role("tab", name="Swarm").click()
    link = tab.locator("#swarm-agents a", has_text="one")
    assert link.get_attribute("href") == "#item-tasks-one"


def test_global_comment_choice_survives_reload_and_the_next_click_collapses(tab):
    doc = {
        **DOC,
        "phases": [
            {"id": "p0", "title": "Review work", "comments": [{"id": "comment", "by": "operator", "text": "Read this"}]}
        ],
        "_meta": {"rev": 10},
    }
    tab.route("**/api/**", lambda route: fulfill_events(route, doc, SWARM))
    tab.reload()
    loaded(tab)
    tab.locator("#comments-all").click()
    assert tab.locator("#comments-all").text_content() == "Hide all comments"
    tab.reload()
    loaded(tab)
    assert tab.locator("#comments-all").text_content() == "Hide all comments"
    tab.locator("#comments-all").click()
    assert tab.locator("#comments-all").text_content() == "Show all comments"
    assert tab.locator("#sec-phases details[data-key]").get_attribute("open") is None


def test_failed_status_read_keeps_the_observed_state_and_reports_the_failure(tab):
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm-state").text_content() == "running"
    tab.route("**/api/v1/**", lambda route: route.fulfill(status=503, body="temporarily unavailable"))
    tab.wait_for_function(
        "document.querySelector('#swarm-note').textContent.includes('Could not read swarm status')", timeout=6000
    )
    assert tab.locator("#swarm-state").text_content() == "running"
    assert tab.locator('[data-swarm="pause"]').is_visible()
    assert "null" not in tab.locator("#tab-swarm").text_content()
    recovered = {**SWARM, "config": {**SWARM["config"], "state": "paused"}}
    tab.route("**/api/v1/**", lambda route: fulfill_events(route, swarm=recovered))
    tab.wait_for_function("document.querySelector('#swarm-state').textContent === 'paused'", timeout=6000)
    assert tab.locator("#swarm-note").text_content() == ""
    tab.route("**/api/v1/**", lambda route: route.fulfill(status=503, body="temporarily unavailable"))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    tab.wait_for_function("document.querySelector('#swarm-note').textContent.includes('Could not read swarm status')")
    assert tab.locator("#swarm-state").text_content() == "unavailable"
    assert "null" not in tab.locator("#tab-swarm").text_content()
    tab.get_by_role("tab", name="Ledger").click()
    tab.locator("#phases input[type=checkbox]").first.check()
    assert "Could not read swarm status" in tab.locator("#swarm-note").text_content()
