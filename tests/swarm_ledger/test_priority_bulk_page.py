from copy import deepcopy

import pytest
from playwright.sync_api import expect

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_priority_titles import DOC, SWARM, URL

browser = chromium_browser


@pytest.fixture
def priorities(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    state = ledger_state(deepcopy(DOC))
    state["priorities"][0]["text"] = "  Priorities   show their item title \n"
    requests = []
    rejected = set()
    conflicts = set()

    def route(request):
        if request.request.url.endswith("/saves"):
            body = request.request.post_data_json
            requests.extend(body["ops"])
            op = body["ops"][0]
            if op.get("target") in conflicts:
                request.fulfill(status=409, json={"error": "revision conflict"})
            else:
                request.fulfill(json={"rejected": [op["id"]] if op.get("target") in rejected else [], "_meta": {"rev": 2}})
        elif "/swarm/" in request.request.url:
            request.fulfill(json=SWARM)
        elif request.request.url.startswith(URL):
            request.fulfill(body=shell_html(), content_type="text/html")
        else:
            request.abort()

    context.route("**/*", route)
    serve_modules(context, state)
    page = context.new_page()
    page.goto(URL + "#ledger")
    loaded(page)
    yield page, requests, rejected, conflicts
    context.close()


def test_only_the_normalized_exact_repeated_ask_is_hidden(priorities):
    page, *_ = priorities
    expect(page.locator("#item-priorities-a1 .prio-title")).to_have_text("Priorities show their item title")
    expect(page.locator("#item-priorities-a1 .prio-text")).to_have_count(0)
    expect(page.locator("#item-priorities-a2 .prio-text")).to_have_text("Choose the first phase")


def test_keyboard_selection_and_deselection_never_act_implicitly(priorities):
    page, requests, *_ = priorities
    select = page.get_by_role("checkbox", name="Select pt1", exact=True)
    select.focus()
    page.keyboard.press("Space")
    expect(select).to_be_checked()
    expect(page.locator(".prio-selected-count")).to_have_text("1 selected")
    page.keyboard.press("Space")
    expect(select).not_to_be_checked()
    expect(page.get_by_role("button", name="Approve selected", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Clear selected", exact=True)).to_be_disabled()
    assert requests == []
    expect(page.locator("#item-priorities-a5 input[type=checkbox]")).to_be_disabled()


def test_bulk_clear_reports_each_result_and_keeps_failed_rows_selected(priorities):
    page, requests, rejected, conflicts = priorities
    rejected.add("a2")
    conflicts.add("a3")
    for label in ("Select pt1", "Select p7", "Select question"):
        page.get_by_role("checkbox", name=label, exact=True).check()
    page.get_by_role("button", name="Clear selected", exact=True).click()
    expect(page.locator(".prio-bulk-results")).to_contain_text("pt1: cleared")
    expect(page.locator(".prio-bulk-results")).to_contain_text("p7: rejected")
    expect(page.locator(".prio-bulk-results")).to_contain_text("question: revision conflict")
    expect(page.get_by_role("checkbox", name="Select pt1", exact=True)).not_to_be_checked()
    expect(page.get_by_role("checkbox", name="Select p7", exact=True)).to_be_checked()
    expect(page.get_by_role("checkbox", name="Select question", exact=True)).to_be_checked()
    assert [op["target"] for op in requests] == ["a1", "a2", "a3"]
    assert all(op["op"] == "priority_clear" for op in requests)


def test_bulk_approve_uses_one_existing_verdict_per_item(priorities):
    page, requests, *_ = priorities
    page.get_by_role("checkbox", name="Select pt1", exact=True).check()
    page.get_by_role("checkbox", name="Select p7", exact=True).check()
    page.get_by_role("button", name="Approve selected", exact=True).dblclick()
    expect(page.locator(".prio-bulk-results")).to_contain_text("p7: approved")
    assert [(op["op"], op["item"], op["verdict"]) for op in requests] == [
        ("verdict", "tasks/pt1", "approved"),
        ("verdict", "phases/p7", "approved"),
    ]
    assert len({op["id"] for op in requests}) == 2
