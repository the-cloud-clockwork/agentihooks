import json
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import (
    ledger_state,
    loaded,
    serve_modules,
    shell_html,
)

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "tests" / "fixtures" / "phase_lifecycle.json").read_text())
URL = "http://ledger.test/phase-rows"
DOC = {
    "title": "Phase rows",
    "overview": "o",
    "phases": [
        {"id": "p1", "title": "Inbox", "description": "first"},
        {"id": "p2", "title": "Seats", "description": "second", "planning": "auto"},
        {"id": "p3", "title": "Release", "description": "third", "depends_on": ["p1", "p2"]},
    ],
    "tasks": [{"id": "plan-p2", "title": "Plan seats", "phase": "p2", "kind": "plan", "lane": "plan", "state": "open"}],
}


def served_answers():
    from scripts.swarm_ledger import ledger_server

    answers = []
    for c in CASES:
        view = ledger_server.page_view(
            {"phases": c["phases"], "tasks": c["tasks"], "_meta": {}}, *ledger_server.NO_DOCTOR
        )
        status = view["phase_status"]
        answers.append([status[c["phase"]]["state"], status[c["phase"]]["label"]])
    return answers


def test_the_served_status_carries_the_same_lifecycle_and_label_as_the_fixture():
    assert served_answers() == [[c["state"], c["label"]] for c in CASES]


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def context(browser):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = shell_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(DOC))
    yield context
    context.close()


def rows(page):
    page.wait_for_selector("#phases > li .phase-state", state="attached")
    return page.evaluate(
        """() => [...document.querySelectorAll("#phases > li")].map((li) => [
          li.querySelector(".phase-state").textContent, li.querySelector(".phase-body .desc").textContent])"""
    )


def test_each_phase_row_shows_its_lifecycle_and_a_waiting_phase_names_its_blockers(context):
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    assert rows(page) == [["Building", "first"], ["Planning", "second"], ["Waits for phase Inbox, Seats", "third"]]


def test_a_phase_row_never_folds_and_always_shows_its_description(context):
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    assert page.locator("#phases details:not([data-key])").count() == 0
    assert page.locator("#item-phases-p3 .phase-body .desc").is_visible()


REVIEW_DOC = {
    **DOC,
    "phases": [{**DOC["phases"][0], "planning": "auto", "review": {"state": "pending"}}, *DOC["phases"][1:]],
    "tasks": [
        *DOC["tasks"],
        {"id": "plan-p1", "title": "Plan inbox", "phase": "p1", "kind": "plan", "lane": "plan", "state": "done"},
    ],
}


@pytest.fixture
def review_page(browser):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = shell_html()
    sent = []

    def answer(route):
        if route.request.url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if route.request.method == "PUT":
            sent.extend(route.request.post_data_json["ops"])
        return route.abort()

    context.route("**/*", answer)
    serve_modules(context, ledger_state(REVIEW_DOC))
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    yield page, sent
    context.close()


def review_ops(page, sent):
    for _ in range(40):
        if sent:
            break
        page.wait_for_timeout(50)
    return [{k: v for k, v in op.items() if k != "id"} for op in sent if op["op"] == "phase_review"]


def test_only_a_phase_in_review_shows_the_review_buttons(review_page):
    page, _ = review_page
    assert page.locator("#item-phases-p1 .phase-review button").all_text_contents() == ["Approve plan", "Send back"]
    assert page.locator("#item-phases-p2 .phase-review, #item-phases-p3 .phase-review").count() == 0


def test_approve_sends_the_review_op_as_the_operator_and_waits_for_the_served_status(review_page):
    page, sent = review_page
    page.click("#item-phases-p1 .phase-review button:has-text('Approve plan')")
    page.wait_for_function(
        "() => document.querySelector('#item-phases-p1 .phase-review').textContent === 'review sent'"
    )
    assert page.locator("#item-phases-p1 .phase-state").text_content() == "Plan in review"
    assert page.locator("#item-phases-p1 .phase-review button").count() == 0
    assert review_ops(page, sent) == [
        {"op": "phase_review", "by": "operator", "item": "phases/p1", "state": "approved"}
    ]


def test_send_back_needs_a_note_and_sends_it(review_page):
    page, sent = review_page
    back = "#item-phases-p1 .phase-review button:has-text('Send back')"
    page.click(back)
    assert page.evaluate("() => document.activeElement.classList.contains('phase-note')")
    assert review_ops(page, sent) == []
    page.fill("#item-phases-p1 .phase-note", "Split the parser task")
    page.click(back)
    page.wait_for_function(
        "() => document.querySelector('#item-phases-p1 .phase-review').textContent === 'review sent'"
    )
    assert review_ops(page, sent) == [
        {
            "op": "phase_review",
            "by": "operator",
            "item": "phases/p1",
            "state": "sent_back",
            "note": "Split the parser task",
        }
    ]


@pytest.mark.parametrize("escalated, shown", [(True, 1), (False, 0)])
def test_a_sent_back_phase_shows_the_buttons_only_once_escalated(browser, escalated, shown):
    review = {"state": "sent_back", "rounds": 3, "escalated": escalated}
    doc = {**REVIEW_DOC, "phases": [{**REVIEW_DOC["phases"][0], "review": review}, *REVIEW_DOC["phases"][1:]]}
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = shell_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(doc))
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    assert page.locator("#item-phases-p1 .phase-review").count() == shown
    context.close()
