"""The Agents table carries one lift button per gate that denied the agent; a click sends the operator's lift."""

import json

from tests.swarm_ledger.test_tabs import SWARM, browser, tab

__all__ = ["browser", "tab"]

GATED = {
    **SWARM,
    "agents": [
        {"name": "master", "lane": "master"},
        {
            "name": "engineer",
            "lane": "eng",
            "task": "one",
            "gates": [{"gate": "talk", "lifted": True}, {"gate": "watch", "lifted": False}],
        },
    ],
}


def open_swarm(tab, puts):
    def route(request):
        if request.request.method == "PUT":
            puts.append(json.loads(request.request.post_data))
        request.fulfill(json=GATED)

    tab.route("**/api/swarm/**", route)
    tab.reload()
    tab.get_by_role("tab", name="Swarm", exact=False).click()


def test_each_active_gate_shows_a_lift_button_or_its_lifted_label(tab):
    open_swarm(tab, [])
    actions = tab.locator("#swarm-agents tr", has_text="engineer").locator(".sw-acts")
    actions.get_by_role("button", name="Lift the watch gate for engineer").wait_for(timeout=3000)
    assert "TALK LIFTED" in actions.inner_text().upper()
    assert actions.get_by_role("button", name="Lift the talk gate for engineer").count() == 0
    assert tab.locator("#swarm-agents tr", has_text="master").locator("[data-lift]").count() == 0


def test_a_click_sends_the_lift_for_that_agent_and_gate(tab):
    puts = []
    open_swarm(tab, puts)
    tab.locator("#swarm-agents tr", has_text="engineer").hover()
    tab.get_by_role("button", name="Lift the watch gate for engineer").click()
    tab.wait_for_function("document.querySelector('#swarm-note').textContent.includes('Lift the gate: done')")
    assert puts == [{"action": "lift", "agent": "engineer", "gate": "watch"}]
