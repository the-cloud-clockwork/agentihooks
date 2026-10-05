from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
DOC = {
    "title": "Sidebar height",
    "overview": "o",
    "phases": [{"id": f"p{n}", "title": f"phase {n}", "description": "d " * 60, "done": False} for n in range(40)],
}


from tests.swarm_ledger.test_tabs import browser, tab

__all__ = ["browser", "tab"]


@pytest.mark.parametrize("width", [1300, 390])
def test_tab_body_ends_at_the_window_bottom_and_side_panels_do_not_scroll(tab, width):
    tab.set_viewport_size({"width": width, "height": 600})
    tab.get_by_role("tab", name="Swarm").click()
    tab.locator("#swarm").evaluate("el => el.scrollTop = 500")
    rect = tab.locator("#swarm").bounding_box()
    assert rect["y"] > 0
    assert rect["y"] + rect["height"] == 600
    assert tab.evaluate("window.scrollY") == 0
    for selector in ["#needs-you-box", "#agents-box", "#capacity-box", "#health-box"]:
        assert tab.locator(selector).evaluate("el => getComputedStyle(el).overflowY") == "visible"
