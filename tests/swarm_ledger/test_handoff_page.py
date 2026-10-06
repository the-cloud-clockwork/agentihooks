from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]


def test_handoff_outcomes_survive_initial_layout_and_resizes(tab):
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    status = {
        **SWARM,
        "transfers": [
            {
                "seat": "eng-1@proof",
                "reason": "recycle",
                "task": "t1",
                "continuity": {"state": "confirmed", "next": "Read the proof."},
                "binding": {"state": "live"},
            }
        ],
    }
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=status))
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    for width in (1440, 390, 1440):
        tab.set_viewport_size({"width": width, "height": 900})
        tab.locator("#swarm-transfers-box").wait_for(state="visible", timeout=1500)
        tab.locator("#swarm-transfers-box").evaluate("element => element.open = true")
        text = tab.locator("#swarm-transfers").inner_text()
        assert "Continuity: confirmed" in text
        assert "Binding: live" in text
    assert errors == []
