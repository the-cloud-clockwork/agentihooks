from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]


def test_handoff_outcomes_survive_initial_layout_and_resizes(tab):
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    status = {
        **SWARM,
        "handoffs": [
            {
                "seat": "eng-1@proof",
                "at": 0,
                "reason": "recycle",
                "continuity": "confirmed",
                "binding": "live",
                "successor": "engineer",
                "awaiting": "",
            }
        ],
    }
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=status))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    for width in (1440, 390, 1440):
        tab.set_viewport_size({"width": width, "height": 900})
        tab.locator("#handoff-box").wait_for(state="visible", timeout=1500)
        text = tab.locator("#swarm-handoffs").inner_text()
        assert "CONFIRMED" in text
        assert "LIVE" in text
        assert "eng 1" in text
    assert errors == []


def test_the_binding_cell_shows_when_the_successor_bound_after_the_handoff(tab):
    handed, bound = 1_791_322_279_893, 1_791_322_399_893
    row = {"seat": "master@proof", "at": handed, "reason": "recycle", "continuity": "pending"}
    status = {
        **SWARM,
        "handoffs": [{**row, "binding": "live", "bound_at": bound, "successor": "master", "awaiting": ""}],
    }
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=status))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    tab.locator("#handoff-box").wait_for(state="visible", timeout=1500)
    times = tab.evaluate(
        "ats => ats.map((at) => new Date(at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}))",
        [handed, bound],
    )
    cells = tab.locator("#swarm-handoffs tr").first.locator("td").all_inner_texts()
    assert cells[1] == times[0]
    assert cells[4] == f"LIVE {times[1]}"
