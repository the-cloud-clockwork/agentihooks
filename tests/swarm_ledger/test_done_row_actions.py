from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
URL = "http://ledger.test/done-rows"
API = "http://ledger.test/api/__LEDGER_SLUG__"
DOC = {
    "title": "Done rows",
    "overview": "o",
    "phases": [
        {"id": "p1", "title": "Seats and continuity", "done": True},
        {"id": "p2", "title": "Intent and plan", "done": False},
        {"id": "p3", "title": "Dropped phase", "done": False, "out_of_scope": True},
    ],
    "questions": [
        {"id": "q1", "text": "Still asked", "answers": []},
        {"id": "q2", "text": "Dropped question", "answers": [], "out_of_scope": True},
    ],
    "tasks": [
        {"id": "t1", "title": "Shipped", "lane": "eng", "state": "done", "done": True},
        {"id": "t2", "title": "Waiting", "lane": "eng", "state": "open", "done": False},
        {"id": "t3", "title": "Dropped task", "lane": "eng", "state": "open", "done": False, "out_of_scope": True},
    ],
    "followups": [
        {"id": "f1", "text": "Still wanted", "done": False},
        {"id": "f2", "text": "Dropped", "done": False, "out_of_scope": True},
        {"id": "f3", "text": "Handled", "done": True},
    ],
}


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
def ledger(browser):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = shell_html()
    served = {"doc": ledger_state(DOC)}

    def answer(route):
        if route.request.url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if is_events(route.request.url):
            return fulfill_events(route, ledger=served["doc"])
        return route.abort()

    context.route("**/*", answer)
    serve_modules(context)
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    page.click("#tasks-box > summary")
    page.click("#tasks-done > summary")
    yield page, served
    context.close()


def verdicts(page, row):
    return page.locator(f"{row} .item-actions .link.approve, {row} .item-actions .link.deny").all_text_contents()


def scope_dot(page, row):
    return page.locator(f"{row} .item-actions .scope")


def serve(page, served, rev, phases, tasks, followups=DOC["followups"]):
    served["doc"] = {**DOC, "phases": phases, "tasks": tasks, "followups": followups, "_meta": {"rev": rev}}


def test_done_phase_and_done_task_offer_no_approve_or_deny(ledger):
    page, _ = ledger
    assert verdicts(page, "#item-phases-p1") == []
    assert verdicts(page, "#item-tasks-t1") == []


def test_open_phase_and_open_task_keep_approve_and_deny(ledger):
    page, _ = ledger
    assert verdicts(page, "#item-phases-p2") == ["Approve", "Deny"]
    assert verdicts(page, "#item-tasks-t2") == ["Approve", "Deny"]


def test_checking_an_open_phase_drops_its_verdicts_and_unchecking_brings_them_back(ledger):
    page, _ = ledger
    page.check("#item-phases-p2 input[type=checkbox]")
    assert verdicts(page, "#item-phases-p2") == []
    page.uncheck("#item-phases-p2 input[type=checkbox]")
    assert verdicts(page, "#item-phases-p2") == ["Approve", "Deny"]


def test_a_live_update_to_done_drops_the_verdicts_and_a_reopen_restores_them(ledger):
    page, served = ledger
    phases = [DOC["phases"][0], {**DOC["phases"][1], "done": True}]
    tasks = [DOC["tasks"][0], {**DOC["tasks"][1], "state": "done", "done": True}]
    serve(page, served, 1, phases, tasks)
    page.wait_for_function("() => document.querySelector('#item-tasks-t2').classList.contains('done')", timeout=6000)
    assert verdicts(page, "#item-phases-p2") == []
    assert verdicts(page, "#item-tasks-t2") == []
    serve(page, served, 2, DOC["phases"], DOC["tasks"])
    page.wait_for_function("() => !document.querySelector('#item-tasks-t2').classList.contains('done')", timeout=6000)
    assert verdicts(page, "#item-phases-p2") == ["Approve", "Deny"]
    assert verdicts(page, "#item-tasks-t2") == ["Approve", "Deny"]


def test_an_out_of_scope_follow_up_shows_only_its_scope_toggle(ledger):
    page, _ = ledger
    assert verdicts(page, "#item-followups-f2") == []
    assert scope_dot(page, "#item-followups-f2").get_attribute("aria-pressed") == "true"


def test_bringing_an_out_of_scope_follow_up_back_restores_approve_and_deny(ledger):
    page, _ = ledger
    scope_dot(page, "#item-followups-f2").click()
    page.wait_for_function(
        "() => !document.querySelector('#item-followups-f2').classList.contains('out')", timeout=6000
    )
    assert verdicts(page, "#item-followups-f2") == ["Approve", "Deny"]
    assert scope_dot(page, "#item-followups-f2").get_attribute("aria-pressed") == "false"


def test_marking_a_follow_up_out_of_scope_drops_its_verdicts(ledger):
    page, _ = ledger
    assert verdicts(page, "#item-followups-f1") == ["Approve", "Deny"]
    page.once("dialog", lambda dialog: dialog.accept())
    scope_dot(page, "#item-followups-f1").click()
    page.wait_for_function("() => document.querySelector('#item-followups-f1').classList.contains('out')", timeout=6000)
    assert verdicts(page, "#item-followups-f1") == []
    assert scope_dot(page, "#item-followups-f1").get_attribute("aria-pressed") == "true"


def test_a_done_follow_up_offers_no_verdicts_and_no_scope_toggle(ledger):
    page, _ = ledger
    page.click("#followups-done > summary")
    assert verdicts(page, "#item-followups-f3") == []
    assert scope_dot(page, "#item-followups-f3").count() == 0


def test_a_live_update_out_of_scope_drops_the_verdicts_and_back_in_scope_restores_them(ledger):
    page, served = ledger
    out = [{**DOC["followups"][0], "out_of_scope": True}, *DOC["followups"][1:]]
    serve(page, served, 1, DOC["phases"], DOC["tasks"], out)
    page.wait_for_function("() => document.querySelector('#item-followups-f1').classList.contains('out')", timeout=6000)
    assert verdicts(page, "#item-followups-f1") == []
    back = [DOC["followups"][0], {**DOC["followups"][1], "out_of_scope": False}, DOC["followups"][2]]
    serve(page, served, 2, DOC["phases"], DOC["tasks"], back)
    page.wait_for_function(
        "() => !document.querySelector('#item-followups-f2').classList.contains('out')", timeout=6000
    )
    assert verdicts(page, "#item-followups-f1") == ["Approve", "Deny"]
    assert verdicts(page, "#item-followups-f2") == ["Approve", "Deny"]


def test_every_out_of_scope_item_list_hides_the_verdicts(ledger):
    page, _ = ledger
    assert verdicts(page, "#item-tasks-t3") == []
    assert scope_dot(page, "#item-tasks-t3").get_attribute("aria-pressed") == "true"
    assert verdicts(page, "#item-phases-p3") == []
    assert verdicts(page, "#item-questions-q2") == []
    assert scope_dot(page, "#item-phases-p3").get_attribute("aria-pressed") == "true"
    assert scope_dot(page, "#item-questions-q2").get_attribute("aria-pressed") == "true"
    assert verdicts(page, "#item-questions-q1") == ["Approve", "Deny"]


def test_bringing_an_out_of_scope_task_back_restores_approve_and_deny(ledger):
    page, _ = ledger
    scope_dot(page, "#item-tasks-t3").click()
    page.wait_for_function("() => !document.querySelector('#item-tasks-t3').classList.contains('out')", timeout=6000)
    assert verdicts(page, "#item-tasks-t3") == ["Approve", "Deny"]
