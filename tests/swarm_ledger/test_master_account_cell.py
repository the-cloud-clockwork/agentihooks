from tests.swarm_ledger.ledger_page import page_source
from tests.swarm_ledger.test_agent_promoted import _rows


def test_only_the_master_row_carries_its_account():
    rows = _rows(
        [
            {"name": "master@a1b2c3-0001", "lane": "master", "status": "working", "account": "roomy"},
            {"name": "engineer@a1b2c3-0002", "lane": "eng", "status": "working", "account": "small", "task": "t1"},
            {"name": "master@a1b2c3-0003", "lane": "master", "status": "finished"},
        ]
    )
    assert [(r["name"], r["account"]) for r in rows] == [
        ("master@a1b2c3-0001", "roomy"),
        ("master@a1b2c3-0003", ""),
        ("engineer@a1b2c3-0002", ""),
    ]


def test_the_master_name_cell_names_its_account():
    assert (
        'a.account ? h("span", {}, idCell(a.name), h("span", { class: "sw-sub", title: `account ${a.account}`, '
        "text: a.account })) : idCell(a.name)" in page_source()
    )
