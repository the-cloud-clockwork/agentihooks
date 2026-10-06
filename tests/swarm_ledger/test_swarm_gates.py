"""The swarm panel's gate mode controls: one enforce, observe, off group per gate, beside the capacity settings."""

from tests.swarm_ledger.test_swarm_layout import browser, open_page, status

__all__ = ["browser", "open_page"]

MODES = {"identity": "enforce", "talk": "observe", "claims": "off"}


def groups(page):
    return page.tab.eval_on_selector_all(
        "#swarm-gates [role=group]",
        "gs => gs.map(g => [g.getAttribute('aria-label'), g.querySelector('.sw-cap-name').textContent,"
        " [...g.querySelectorAll('button')].map(b => [b.textContent, b.getAttribute('aria-pressed')])])",
    )


def test_every_gate_shows_its_current_mode_inside_the_capacity_row(open_page):
    page = open_page(status(gate_modes=MODES))
    assert page.tab.locator("#capacity-box #swarm-gates").count() == 1
    pressed = {
        "enforce": ["true", "false", "false"],
        "observe": ["false", "true", "false"],
        "off": ["false", "false", "true"],
    }
    assert groups(page) == [
        [f"{name} gate mode", name, [[m, p] for m, p in zip(("enforce", "observe", "off"), pressed[mode])]]
        for name, mode in MODES.items()
    ]


def test_a_click_sets_that_gate_mode_through_the_swarm_control(open_page):
    page = open_page(status(gate_modes=MODES))
    page.tab.locator('#swarm-gates [aria-label="identity gate mode"] button', has_text="observe").click()
    page.tab.wait_for_function("() => !document.querySelector('#swarm-gates button').disabled")
    assert page.puts == [{"action": "set", "gates": {"identity": "observe"}}]
    assert page.text("#swarm-note") == "Set gate mode: done"


def test_a_status_without_gate_modes_shows_no_gate_controls(open_page):
    page = open_page(status())
    assert page.tab.locator("#swarm-gates button").count() == 0
    assert not page.tab.locator("#gates-label").is_visible()
