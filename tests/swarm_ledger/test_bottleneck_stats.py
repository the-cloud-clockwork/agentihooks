import pytest

from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]


def test_top_stage_trend_and_action_fit_the_operator_viewport(open_page):
    page = open_page(
        status(
            top_bottleneck={
                "stage": "ci_overrun",
                "label": "ci overrun",
                "trend": "rising",
                "last_action": "Proposed a faster mutation run",
                "value": 3,
                "threshold": 1,
                "unit": "minutes",
            }
        ),
        width=1920,
    )
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    box = page.tab.locator("#stats .top-bottleneck")
    assert box.is_visible()
    assert box.inner_text() == "Top bottleneck\nci overrun\nTrend rising\nLast action Proposed a faster mutation run"
    bounds = box.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 1920
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 1080


@pytest.mark.parametrize("top,label", [(None, "unavailable"), ({"stage": ""}, "none")])
def test_stats_distinguish_missing_status_from_no_active_finding(open_page, top, label):
    page = open_page(status(top_bottleneck=top), width=1920)
    assert label in page.tab.locator("#stats .top-bottleneck").inner_text()


def test_action_is_text_and_long_actions_wrap_within_stats(open_page):
    action = '<img src=x onerror="window.injected=true"> ' + "Proposed a remedy " * 8
    page = open_page(
        status(top_bottleneck={"stage": "build", "label": "build", "trend": "steady", "last_action": action}),
        width=1920,
    )
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    box = page.tab.locator("#stats .top-bottleneck")
    assert action in box.inner_text()
    assert box.locator("img").count() == 0
    assert box.evaluate("e => e.scrollWidth <= e.clientWidth")
