import json

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
URL = "http://ledger.test/architecture-page"
SWARM = {"config": {"slug": "s", "state": "running", "max_eng": 1, "max_ci": 1}, "agents": [], "tasks": {}}
VIEW = {
    "revision": 2,
    "components": 13,
    "proposals": [
        {"id": "duplicate-dispatcher", "name": "Kubernetes task dispatcher", "kind": "dispatcher", "approved": False}
    ],
    "revisions": [{"revision": 1, "operation": ""}],
}
APPROVED = {**VIEW, "proposals": [{**VIEW["proposals"][0], "approved": True}]}


@pytest.fixture
def page(browser, request):
    answer = getattr(request, "param", (200, {"data": APPROVED}))
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()
    sent = []

    def route(call):
        url = call.request.url
        if is_events(url):
            return fulfill_events(call, ledger_state({"title": "Architecture"}), SWARM)
        if url.endswith("/architecture"):
            return call.fulfill(json={"data": VIEW})
        if url.endswith("/architecture/actions"):
            sent.append({"body": call.request.post_data_json, "token": call.request.headers.get("x-ledger-token")})
            return call.fulfill(status=answer[0], body=json.dumps(answer[1]), content_type="application/json")
        if url.startswith(URL):
            return call.fulfill(body=html, content_type="text/html")
        return call.abort()

    context.route("**/*", route)
    serve_modules(context)
    tab = context.new_page()
    tab.set_default_timeout(3000)
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.goto(URL)
    loaded(tab)
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    tab.locator("#architecture-box").wait_for(state="visible")
    tab.locator("#swarm-architecture tr").first.wait_for()
    yield tab, sent
    assert errors == []
    context.close()


def rows(tab):
    return [row.locator("td").all_inner_texts() for row in tab.locator("#swarm-architecture tr").all()]


def sent_body(tab, sent):
    for _ in range(40):
        if sent:
            break
        tab.wait_for_timeout(50)
    return sent


def test_the_panel_offers_approve_on_a_proposal_and_roll_back_on_an_earlier_revision(page):
    tab, _ = page
    assert tab.locator("#architecture-count").text_content() == "revision 2"
    table = rows(tab)
    assert [cells[:3] for cells in table] == [
        ["Kubernetes task dispatcher", "proposal", "AWAITING APPROVAL"],
        ["revision 1", "revision", "initial record"],
    ]
    assert tab.locator("#swarm-architecture [data-architecture='approve']").inner_text() == "approve"
    assert tab.locator("#swarm-architecture [data-architecture='rollback']").inner_text() == "roll back"


def test_approve_sends_the_proposal_and_reason_with_the_page_credential(page):
    tab, sent = page
    token = tab.evaluate("() => document.querySelector('meta[name=\"ledger-token\"]').content")
    tab.fill("#swarm-architecture input[data-reason='duplicate-dispatcher']", "operator moves dispatch")
    tab.click("#swarm-architecture [data-architecture='approve']")
    assert sent_body(tab, sent) == [
        {
            "body": {"action": "approve", "proposal": "duplicate-dispatcher", "reason": "operator moves dispatch"},
            "token": token,
        }
    ]
    tab.wait_for_function("() => document.querySelector('#swarm-architecture').innerText.includes('APPROVED')")
    assert tab.locator("#swarm-architecture [data-architecture='approve']").count() == 0


def test_approve_waits_for_a_reason(page):
    tab, sent = page
    assert tab.locator("#swarm-architecture [data-architecture='approve']").is_disabled()
    tab.fill("#swarm-architecture input[data-reason='duplicate-dispatcher']", "why")
    assert tab.locator("#swarm-architecture [data-architecture='approve']").is_enabled()
    assert sent == []


def test_roll_back_sends_the_target_revision(page):
    tab, sent = page
    tab.click("#swarm-architecture [data-architecture='rollback']")
    assert sent_body(tab, sent)[0]["body"] == {"action": "rollback", "to": 1}


@pytest.mark.parametrize(
    "page",
    [(403, {"error": {"code": "forbidden", "message": "Architecture changes need the operator"}})],
    indirect=True,
)
def test_a_refused_action_shows_its_reason_and_keeps_the_record(page):
    tab, sent = page
    tab.click("#swarm-architecture [data-architecture='rollback']")
    sent_body(tab, sent)
    tab.wait_for_function("() => document.querySelector('#architecture-status').innerText !== ''")
    assert tab.locator("#architecture-status").inner_text() == (
        "Could not roll back: Architecture changes need the operator."
    )
    assert tab.locator("#architecture-count").text_content() == "revision 2"
