from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest

from scripts.swarm.health import order_check
from tests.swarm_ledger.ledger_page import (
    fulfill_events,
    is_events,
    ledger_state,
    loaded,
    reload_after_folds,
    serve_modules,
    shell_html,
)
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
    assert (
        row.locator(".task-meta span")
        .nth(2)
        .evaluate(
            "(element) => { const probe = document.createElement('span'); probe.style.color = 'var(--positive)'; element.append(probe); const expected = getComputedStyle(probe).color; probe.remove(); return getComputedStyle(element).color === expected; }"
        )
    )


@pytest.mark.feature("ledger-orders-panel")
@pytest.mark.parametrize("orders_page", ["empty"], indirect=True)
def test_no_open_order_has_an_explicit_empty_state(orders_page: Any) -> None:
    tab, _ = orders_page
    tab.locator("#orders .empty").wait_for()
    assert tab.locator("#orders").inner_text() == "No open operator orders."


@pytest.mark.feature("ledger-orders-panel")
def test_every_open_order_is_rendered_oldest_first_with_safe_text(orders_page: Any) -> None:
    tab, doc = orders_page
    tab.locator("#orders > li .item-title").wait_for()
    doc["phases"].append(
        {"id": "earlier", "title": "Earlier <script>order</script>", "ordered": {"at": 2, "by": "operator"}}
    )
    tab.wait_for_function("() => document.querySelectorAll('#orders > li .item-title').length === 2")
    rows = tab.locator("#orders > li")
    assert rows.locator(".item-title").all_text_contents() == [
        "Earlier <script>order</script>",
        "Finish the operator console",
    ]
    assert rows.nth(0).locator(".task-meta span").all_text_contents() == ["0 claimable", "0 busy", "0 done"]
    assert rows.nth(0).locator("blockquote").count() == 0
    assert rows.locator("script").count() == 0


@pytest.mark.feature("ledger-orders-panel")
def test_order_counts_refresh_and_closed_orders_leave_the_panel(orders_page: Any) -> None:
    tab, doc = orders_page
    tab.locator("#orders > li .item-title").wait_for()
    doc["tasks"][0]["state"] = "claimed"
    tab.wait_for_function(
        "() => document.querySelector('#orders .task-meta').textContent === '0 claimable2 busy1 done'"
    )
    assert tab.locator("#orders .task-meta span").all_text_contents() == ["0 claimable", "2 busy", "1 done"]
    doc["phases"][0]["done"] = True
    tab.locator("#orders .empty").wait_for()
    assert tab.locator("#orders").inner_text() == "No open operator orders."
    assert tab.locator("#orders-count").inner_text() == "· 0 OPEN"


@pytest.mark.feature("ledger-orders-panel")
def test_failed_read_preserves_orders_and_recovers_on_the_next_update(orders_page: Any) -> None:
    tab, doc = orders_page
    tab.locator("#orders > li .item-title").wait_for()

    def refuse(call: Any) -> None:
        call.fulfill(status=503, json={"error": {"message": "Unavailable"}})

    tab.context.route("**/orders", refuse)
    doc["overview"] = "An update while the orders read is unavailable."
    tab.wait_for_function(
        "() => document.querySelector('#orders-status').textContent === 'Operator orders unavailable.'"
    )
    assert tab.locator("#orders .item-title").inner_text() == "Finish the operator console"
    assert tab.locator("#orders .empty").count() == 0
    tab.context.unroute("**/orders", refuse)
    doc["tasks"][0]["state"] = "done"
    tab.wait_for_function(
        "() => document.querySelector('#orders .task-meta').textContent === '0 claimable1 busy2 done'"
    )
    assert tab.locator("#orders-status").inner_text() == ""


@pytest.mark.feature("ledger-orders-panel")
def test_order_panel_fold_choice_survives_reload(orders_page: Any) -> None:
    tab, _ = orders_page
    tab.locator("#orders > li .item-title").wait_for()
    tab.locator("#orders-box > summary").click()
    assert not tab.locator("#orders-box").evaluate("(box) => box.open")
    reload_after_folds(tab, "#orders-box")
    assert not tab.locator("#orders-box").evaluate("(box) => box.open")
    assert tab.locator("#sec-orders").count() == 1
