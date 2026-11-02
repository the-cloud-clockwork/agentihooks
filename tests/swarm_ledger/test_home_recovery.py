import json

import pytest
from playwright.sync_api import expect

pytest_plugins = ["tests.swarm_ledger.test_ledger_server_home_rows"]


@pytest.mark.parametrize("view,columns", [("home", 8), ("bin", 6)])
def test_columns_and_navigation_are_accessible(context, view, columns):
    page = context.new_page()
    page.goto("/" if view == "home" else "/?view=bin")
    table = page.get_by_role("table")
    expect(table.get_by_role("columnheader")).to_have_count(columns)
    expect(table.get_by_role("row").nth(1).get_by_role("cell")).to_have_count(columns)
    expect(page.get_by_role("navigation").get_by_role("link")).to_have_count(1)
    if view == "home":
        expect(table.get_by_role("columnheader", name="Activity")).to_have_attribute("aria-sort", "descending")
        page.get_by_role("button", name="Kind", exact=True).click()
        expect(table.get_by_role("columnheader", name="Kind")).to_have_attribute("aria-sort", "ascending")
        page.reload()
        expect(table.get_by_role("columnheader", name="Kind")).to_have_attribute("aria-sort", "ascending")


@pytest.mark.parametrize("failure", ["network", "server"])
def test_collection_failures_offer_retry_without_raw_errors(context, failure):
    page = context.new_page()
    route = "**/api/v1/ledgers?*"
    handler = (
        (lambda r: r.abort())
        if failure == "network"
        else (lambda r: r.fulfill(status=500, body="private server traceback"))
    )
    page.route(route, handler)
    page.goto("/")
    expect(page.get_by_role("status")).to_contain_text("could not be loaded")
    expect(page.locator("body")).not_to_contain_text("private server traceback")
    page.unroute(route, handler)
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator("li.row")).to_have_count(7)


@pytest.mark.parametrize("failure", ["network", "server"])
def test_action_failure_restores_button_and_retry(context, failure):
    page = context.new_page()
    route = "**/api/v1/bin/actions"
    handler = (
        (lambda r: r.abort())
        if failure == "network"
        else (lambda r: r.fulfill(status=500, body="private server traceback"))
    )
    page.route(route, handler)
    page.goto("/")
    button = page.get_by_role("button", name="Move Ledger b to the bin", exact=True)
    button.click()
    expect(page.get_by_role("status")).to_contain_text("could not be moved")
    expect(button).to_be_enabled()
    expect(page.locator("body")).not_to_contain_text("private server traceback")
    page.unroute(route, handler)
    calls = []
    page.route(route, lambda r: (calls.append(r.request.post_data_json), r.fulfill(json={"ok": True})))
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.get_by_role("button", name="Undo", exact=True)).to_be_visible()
    assert calls == [{"action": "delete", "slug": "b"}]


def test_undo_retries_restore_of_same_ledger_without_swarm_command(context):
    page = context.new_page()
    calls = []
    fail_restore = [True]

    def action(route):
        body = route.request.post_data_json
        calls.append(body)
        if body["action"] == "restore" and fail_restore[0]:
            route.fulfill(status=500, body="private failure")
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/v1/bin/actions", action)
    swarm_calls = []
    page.on("request", lambda r: swarm_calls.append(r.url) if "/swarm/controls" in r.url else None)
    page.goto("/")
    page.get_by_role("button", name="Move Ledger b to the bin", exact=True).click()
    page.get_by_role("button", name="Undo", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("could not be restored")
    fail_restore[0] = False
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("restored to HOME")
    assert calls == [{"action": "delete", "slug": "b"}, *[{"action": "restore", "slug": "b"}] * 2]
    assert swarm_calls == []


@pytest.mark.parametrize("view", ["home", "bin"])
def test_empty_views_explain_supported_actions(context, view):
    page = context.new_page()
    for name in ("ledgers", "bin"):
        page.route(f"**/api/v1/{name}?*", lambda r: r.fulfill(json={"data": [], "next_cursor": None}))
    page.goto("/" if view == "home" else "/?view=bin")
    if view == "bin":
        expect(page.get_by_text("30 days", exact=False)).to_be_visible()
        expect(page.get_by_role("link", name="Go to HOME")).to_be_visible()
    else:
        expect(page.locator("code")).to_contain_text("agentihooks ledger new")


def test_reduced_motion_removes_all_home_transitions(context):
    page = context.new_page()
    page.emulate_media(reduced_motion="reduce")
    page.goto("/")
    page.wait_for_selector("li.row")
    moving = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(el => {
      const style = getComputedStyle(el);
      return style.transitionDuration.split(',').some(v => parseFloat(v) > 0)
        || style.animationDuration.split(',').some(v => parseFloat(v) > 0);
    }).map(el => el.className)""")
    assert json.dumps(moving) == "[]"
