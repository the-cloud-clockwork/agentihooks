import json
from pathlib import Path
from urllib.parse import unquote

from scripts.swarm import context_view as cv
from tests.swarm_ledger import test_swarm_layout as layout
from tests.swarm_ledger.test_swarm_layout import status

browser = layout.browser
open_page = layout.open_page

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "swarm_v2" / "context-evidence.json").read_text())
TASK = FIXTURE["task"]


def payload(revoked=False):
    evidence = {ref: json.dumps({**held, "ref": ref}) for ref, held in FIXTURE["evidence"].items()}
    if revoked:
        evidence["session:s-41"] = json.dumps({"state": "revoked", "reason": FIXTURE["revoked"]["session:s-41"]})
    card = cv.card(TASK, FIXTURE["pack"], evidence, layout.NOW_MS)
    return status(context={"cards": [card], cv.METRIC: {}})


def answer(page, replies):
    def fulfill(route):
        ref = unquote(route.request.url.rsplit("/", 1)[1])
        code, body = replies[ref]
        route.fulfill(status=code, json=body)

    page.tab.route("**/swarm/context/**", fulfill)


def item(page, n):
    return page.tab.locator(f'[id="context-{TASK}"] .ctx-item').nth(n)


def test_the_context_row_shows_brain_purpose_receipt_delivery_and_labelled_items(open_page):
    page = open_page(payload())
    row = page.table("swarm-context")[0]
    assert row[:5] == [TASK, "swarm:rig", "task start", "FRESH", "claude delivered\ncodex unsupported_event"]
    assert page.text("#context-count").lower() == "1 task"
    assert "BRAIN SYNTHESIS" in item(page, 0).inner_text()
    assert "INFERRED INTENT" in item(page, 1).inner_text()
    assert "SUPERSEDED BY DECISION:QUEUE-V2" in item(page, 2).inner_text()
    assert "TRANSCRIPT EVIDENCE" in item(page, 3).inner_text()
    assert "1 omitted" in row[5] and "arc:old-ci-speed" not in row[5]


def test_an_item_from_another_brain_shows_no_reference_summary_or_evidence_button(open_page):
    page = open_page(payload())
    private = item(page, 4)
    assert cv.OUTSIDE in private.inner_text()
    assert "personal session" not in page.text("#swarm-context")
    assert private.locator("button").count() == 0


def test_evidence_opens_inline_and_a_refusal_shows_unavailable(open_page):
    page = open_page(payload())
    session = {"data": {**FIXTURE["evidence"]["session:s-41"], "ref": "session:s-41"}}
    gone = {"error": {"code": "revoked", "message": "the session was deleted by its owner"}}
    answer(page, {"session:s-41": (200, session), "arc:merge-queue": (410, gone)})
    item(page, 3).locator("button").click()
    item(page, 3).locator(".ctx-evidence").wait_for()
    assert "records 120 to 124 of session s-41" in item(page, 3).inner_text()
    assert "queue refused: head is stale" in item(page, 3).inner_text()
    item(page, 0).locator("button").click()
    item(page, 0).locator(".ctx-evidence").wait_for()
    assert "unavailable: the session was deleted by its owner" in item(page, 0).inner_text()


def test_an_unreachable_ledger_shows_unavailable_on_the_item(open_page):
    page = open_page(payload())
    page.tab.route("**/swarm/context/**", lambda route: route.abort())
    item(page, 0).locator("button").click()
    item(page, 0).locator(".ctx-evidence").wait_for()
    assert "unavailable: the ledger could not be reached" in item(page, 0).inner_text()


def test_a_revoked_source_replaces_opened_evidence_with_its_reason(open_page):
    page = open_page(payload())
    session = {"data": {**FIXTURE["evidence"]["session:s-41"], "ref": "session:s-41"}}
    answer(page, {"session:s-41": (200, session)})
    item(page, 3).locator("button").click()
    item(page, 3).locator(".ctx-evidence").wait_for()
    page.payload = payload(revoked=True)
    page.tab.reload()
    layout.loaded(page.tab)
    item(page, 3).wait_for()
    text = item(page, 3).inner_text()
    assert "unavailable: the session was deleted by its owner" in text
    assert "queue refused" not in text
    assert item(page, 3).locator("button").count() == 0


def test_without_context_the_table_says_so(open_page):
    page = open_page(status())
    assert page.table("swarm-context") == [["No task has been given brain context yet."]]
    assert page.text("#context-count").lower() == "none delivered"


def test_with_inspection_off_the_panel_and_its_grip_are_hidden(open_page):
    page = open_page(status(context=None))
    assert page.tab.locator("#context-box").is_hidden()
    assert page.tab.locator('[data-row="context-box"]').is_hidden()
    assert page.tab.locator("#recovery-box").is_visible()
