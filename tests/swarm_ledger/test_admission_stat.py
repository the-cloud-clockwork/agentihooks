import pytest

from tests.swarm_ledger.ledger_page import chromium, ledger_state, shell_html, show

DOC = {"title": "Admission", "overview": "o", "phases": []}
STATUS = {"config": {"state": "running", "max_eng": 3, "max_ci": 1}, "agents": [], "tasks": {}, "findings": []}


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


def stat_text(browser, swarm):
    tab = browser.new_page(viewport={"width": 1920, "height": 1080})
    try:
        show(tab, shell_html(), ledger=ledger_state(DOC), swarm=swarm)
        row = tab.locator('#stats [data-stat="admission-refusals"]')
        row.wait_for()
        return row.locator("dt").text_content().strip(), row.locator("dd").text_content().strip()
    finally:
        tab.close()


def test_stats_show_admission_refusals_counted_by_reason(browser):
    swarm = {**STATUS, "admission": {"cpu": 2, "ram": 1, "missing telemetry": 1}}
    assert stat_text(browser, swarm) == ("Admission refusals", "cpu 2 · ram 1 · missing telemetry 1")


def test_stats_show_none_when_nothing_is_refused(browser):
    assert stat_text(browser, {**STATUS, "admission": {}}) == ("Admission refusals", "none")
    assert stat_text(browser, STATUS) == ("Admission refusals", "none")
