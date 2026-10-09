import json
import subprocess
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, page_source, serve_modules, shell_html

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "swarm_ledger" / "template.html"
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


def function_source(name):
    page = page_source()
    return f"function {name}(" + page.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def page_answers():
    source = "\n".join(function_source(name) for name in ("phaseLifecycle", "phaseLabel"))
    script = (
        f"const PHASE_LABELS = {page_constant('PHASE_LABELS')};\n{source}\nconst cases = {json.dumps(CASES)};\n"
        "process.stdout.write(JSON.stringify(cases.map((c) => {\n"
        "  const phase = c.phases.find((p) => p.id === c.phase), doc = { phases: c.phases, tasks: c.tasks };\n"
        "  return [phaseLifecycle(phase, doc), phaseLabel(phase, doc)];\n"
        "})));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def page_constant(name):
    page = page_source()
    return page.split(f"  const {name} = ", 1)[1].split(";\n", 1)[0]


def test_the_page_derives_the_same_lifecycle_and_label_as_the_fixture():
    assert page_answers() == [[c["state"], c["label"]] for c in CASES]


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
    loaded(page)
    return page.evaluate(
        """() => [...document.querySelectorAll("#phases > li")].map((li) => {
          const fold = li.querySelector("details.phase-fold");
          return [li.querySelector(".phase-state").textContent, fold.open, fold.querySelector(".desc").textContent];
        })"""
    )


def test_each_phase_row_shows_its_lifecycle_and_a_waiting_phase_names_its_blockers(context):
    page = context.new_page()
    page.goto(URL)
    assert rows(page) == [
        ["Building", True, "first"],
        ["Planning", True, "second"],
        ["Waits for phase Inbox, Seats", True, "third"],
    ]


def test_a_folded_phase_row_stays_folded_after_a_reload(context):
    page = context.new_page()
    page.goto(URL)
    page.click("#item-phases-p3 details.phase-fold > summary")
    page.wait_for_function(
        """() => Object.keys(localStorage).some((k) => k.endsWith(":comments")
          && JSON.parse(localStorage.getItem(k)).closed.includes("phases/p3/row"))"""
    )
    page.reload()
    assert [open_ for _, open_, _ in rows(page)] == [True, True, False]


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


def test_approve_sends_the_review_op_as_the_operator(review_page):
    page, sent = review_page
    page.click("#item-phases-p1 .phase-review button:has-text('Approve plan')")
    page.wait_for_function("() => document.querySelector('#item-phases-p1 .phase-state').textContent === 'Building'")
    assert page.locator("#item-phases-p1 .phase-review").count() == 0
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
    page.wait_for_function("() => document.querySelector('#item-phases-p1 .phase-review') === null")
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
    assert page.locator("#item-phases-p1 .phase-review").count() == shown
    context.close()
