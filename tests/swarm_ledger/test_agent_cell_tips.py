from tests.swarm_ledger.test_swarm_layout import browser, open_page, status  # noqa: F401


def test_a_two_label_agent_cell_carries_its_full_text_as_one_ledger_tip(open_page):  # noqa: F811
    payload = status()
    payload["agents"][0]["capabilities"] = {"push_inbox": True, "native_terminal_attach": False}
    payload["agents"][1]["capabilities"] = {"push_inbox": False, "native_terminal_attach": True}
    payload["agents"][1]["promoted"] = True
    page = open_page(payload, width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    found = page.tab.eval_on_selector_all(
        "#swarm-agents :is(.sw-transport, .sw-promoted)",
        "els => els.map(el => [el.className, window.ledgerTip(el), el.getAttribute('title')])",
    )
    assert found == [
        ["sw-transport", "push · no native terminal", None],
        ["sw-transport", "no push · native terminal", None],
        ["sw-promoted", "idle · promoted", None],
    ]
