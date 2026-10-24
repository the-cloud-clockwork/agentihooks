import pytest

from tests.swarm_ledger.ledger_page import chromium, ledger_state, shell_html, show

DOC = {"title": "Admission", "overview": "o", "phases": []}
STATUS = {"config": {"state": "running", "max_eng": 3, "max_ci": 1}, "agents": [], "tasks": {}, "findings": []}
TOP = {"label": "seat wait", "trend": "rising", "last_action": "none recorded", "waits": ["Seat wait 2.0 task hours"]}


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


def bottleneck_lines(browser, swarm):
    tab = browser.new_page(viewport={"width": 1920, "height": 1080})
    try:
        show(tab, shell_html(), ledger=ledger_state(DOC), swarm=swarm)
        stat = tab.locator('#stats [data-stat="top-bottleneck"]')
        stat.wait_for()
        assert tab.locator('#stats [data-stat="admission-refusals"]').count() == 0
        return [item.text_content() for item in stat.locator("dd li").all()]
    finally:
        tab.close()


def test_top_bottleneck_lists_admission_refusals_counted_by_reason(browser):
    swarm = {**STATUS, "top_bottleneck": TOP, "admission": {"cpu": 2, "ram": 1, "missing telemetry": 1}}
    assert bottleneck_lines(browser, swarm)[-2:] == [
        "Seat wait 2.0 task hours",
        "Admission refusals cpu 2 · ram 1 · missing telemetry 1",
    ]


def test_refusals_show_without_a_measured_bottleneck(browser):
    assert bottleneck_lines(browser, {**STATUS, "admission": {"ram": 3}})[-1] == "Admission refusals ram 3"


def test_no_refusal_adds_no_line(browser):
    lines = bottleneck_lines(browser, {**STATUS, "top_bottleneck": TOP, "admission": {}})
    assert lines[-1] == "Seat wait 2.0 task hours"
    assert bottleneck_lines(browser, STATUS) == ["unavailable", "Trend unknown", "Last action none recorded"]
