import json
import subprocess
from pathlib import Path

import pytest

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
    page = TEMPLATE.read_text(encoding="utf-8")
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
    page = TEMPLATE.read_text(encoding="utf-8")
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
    html = TEMPLATE.read_text(encoding="utf-8").replace("__LEDGER_DATA__", json.dumps(DOC))
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    yield context
    context.close()


def rows(page):
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
