import pytest
from playwright.sync_api import expect

from tests.swarm_ledger import test_swarm_layout as layout

browser = layout.browser
open_page = layout.open_page

LANES = ("eng", "ci", "plan")


def scaling_page(open_page, scaling, ceilings=None):
    payload = layout.status()
    payload["config"]["scaling"] = scaling
    if ceilings:
        payload["quota_capacity"] = {"autoscale": {"ceilings": ceilings}}
    return open_page(payload, width=1920)


def pressed(page):
    return page.tab.eval_on_selector_all(
        "#swarm-scaling button", "bs => bs.map(b => [b.textContent, b.getAttribute('aria-pressed')])"
    )


def test_auto_scaling_shows_the_ceilings_read_only_with_no_steppers(open_page):
    page = scaling_page(open_page, "auto", {"eng": 6, "ci": 2, "plan": 1})
    assert pressed(page) == [["auto", "true"], ["manual", "false"]]
    for lane, value in zip(LANES, ("6", "2", "1"), strict=True):
        field = page.tab.locator(f"#cap-{lane}")
        expect(field).to_have_value(value)
        assert field.evaluate("f => f.readOnly")
        for step in ("down", "up"):
            expect(page.tab.locator(f'[data-swarm="{lane}_{step}"]')).to_be_hidden()
    expect(page.tab.locator("#cap-masters")).to_be_editable()


def caps_line(page):
    return [
        text.split("/")[1]
        for text in page.tab.eval_on_selector_all("#alert-live span", "ss => ss.map(s => s.textContent)")
    ]


@pytest.mark.parametrize("scaling,expected", [("auto", ["6", "2", "1"]), ("manual", ["3", "1", "1"])])
def test_the_live_line_counts_against_the_ceiling_in_auto_and_the_cap_in_manual(open_page, scaling, expected):
    page = scaling_page(open_page, scaling, {"eng": 6, "ci": 2, "plan": 1})
    assert caps_line(page) == expected


def test_auto_without_a_decision_reads_the_configured_caps(open_page):
    page = scaling_page(open_page, "auto")
    expect(page.tab.locator("#cap-eng")).to_have_value("3")
    assert page.tab.locator("#cap-eng").evaluate("f => f.readOnly")


def test_manual_scaling_keeps_the_caps_editable(open_page):
    page = scaling_page(open_page, "manual", {"eng": 6, "ci": 2, "plan": 1})
    assert pressed(page) == [["auto", "false"], ["manual", "true"]]
    expect(page.tab.locator("#cap-eng")).to_have_value("3")
    expect(page.tab.locator("#cap-eng")).to_be_editable()
    expect(page.tab.locator('[data-swarm="eng_up"]')).to_be_visible()


def test_the_switch_sends_the_scaling_mode(open_page):
    page = scaling_page(open_page, "auto", {"eng": 6, "ci": 2, "plan": 1})
    page.tab.locator('#swarm-scaling button[data-scaling="manual"]').click()
    for _ in range(100):
        if page.puts:
            break
        page.tab.wait_for_timeout(100)
    assert page.puts == [{"action": "set", "scaling": "manual"}]
