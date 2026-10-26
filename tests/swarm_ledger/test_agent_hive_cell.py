from tests.swarm_ledger.test_swarm_layout import browser, open_page, status  # noqa: F401


def test_the_agents_table_names_each_agents_hive_under_its_name(open_page):  # noqa: F811
    payload = status()
    payload["agents"][0]["hive"] = "workstation"
    payload["agents"][1]["hive"] = "desk"
    page = open_page(payload, width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    subs = page.tab.eval_on_selector_all(
        "#swarm-agents tr td:first-child .sw-sub", "els => els.map(el => [el.closest('tr').id, el.textContent])"
    )
    assert subs == [["agent-master-1", "luna"], ["agent-master-1", "workstation"], ["agent-eng-58", "desk"]]


def test_an_agent_without_a_hive_shows_no_hive_line(open_page):  # noqa: F811
    page = open_page(status(), width=1920)
    assert page.tab.eval_on_selector_all("#agent-eng-58 td:first-child .sw-sub", "els => els.length") == 0
