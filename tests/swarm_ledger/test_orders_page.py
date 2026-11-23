from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest

from scripts.swarm.health import order_check
from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
URL = "http://ledger.test/orders-page"
SWARM = {"config": {"slug": "s", "state": "running", "max_eng": 1, "max_ci": 1}, "agents": [], "tasks": {}}
DOC = {
    "title": "Operator orders",
    "overview": "Work directed by the operator.",
    "phases": [
        {
            "id": "build",
            "title": "Finish the operator console",
            "ordered": {"at": 7, "by": "operator", "quote": 'Show every order, including <pending> & "busy".'},
        }
    ],
    "tasks": [
        {"id": "ready", "title": "Ready work", "phase": "build", "lane": "eng", "state": "open"},
        {"id": "active", "title": "Active work", "phase": "build", "lane": "eng", "state": "claimed"},
        {"id": "finished", "title": "Finished work", "phase": "build", "lane": "eng", "state": "done"},
    ],
}


@pytest.fixture
def orders_page(browser: Any, request: pytest.FixtureRequest) -> Iterator[tuple[Any, dict[str, Any]]]:
    doc = deepcopy(DOC)
    if getattr(request, "param", "ordered") == "empty":
        del doc["phases"][0]["ordered"]
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()

    def route(call: Any) -> None:
        url = call.request.url
        if is_events(url):
            return fulfill_events(call, ledger_state(doc), SWARM)
        if url.endswith("/orders"):
            return call.fulfill(json={"data": order_check.read(doc, ""), "revision": "orders", "next_cursor": None})
        if url.startswith(URL):
            return call.fulfill(body=html, content_type="text/html")
        return call.abort()

    context.route("**/*", route)
    serve_modules(context)
    tab = context.new_page()
    tab.set_default_timeout(3000)
    errors: list[str] = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.goto(URL)
    loaded(tab)
    yield tab, doc
    assert errors == []
    context.close()


@pytest.mark.feature("ledger-orders-panel")
def test_existing_phase_and_overview_remain_visible(orders_page: Any) -> None:
    tab, _ = orders_page
    assert tab.locator("#overview").inner_text() == "Work directed by the operator."
    assert tab.locator("#phases .item-title").inner_text() == "Finish the operator console"


@pytest.mark.feature("ledger-orders-panel")
def test_ordered_phase_shows_quoted_words_and_master_counts(orders_page: Any) -> None:
    tab, _ = orders_page
    row = tab.locator("#orders > li")
    row.wait_for()
    assert row.locator(".item-title").inner_text() == "Finish the operator console"
    assert row.locator("blockquote").inner_text() == 'Show every order, including <pending> & "busy".'
    assert row.locator(".task-meta span").all_text_contents() == ["1 claimable", "1 busy", "1 done"]
    assert row.locator("pending").count() == 0


@pytest.mark.feature("ledger-orders-panel")
@pytest.mark.parametrize("orders_page", ["empty"], indirect=True)
def test_no_open_order_has_an_explicit_empty_state(orders_page: Any) -> None:
    tab, _ = orders_page
    tab.locator("#orders .empty").wait_for()
    assert tab.locator("#orders").inner_text() == "No open operator orders."
