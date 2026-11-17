import pytest

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


@pytest.mark.feature("balance-master-route")
def test_the_master_row_carries_the_route_its_status_names() -> None:
    rows = _rows(
        [
            {
                "name": "master@a1b2c3-0001",
                "lane": "master",
                "status": "working",
                "account": "roomy",
                "route": "claude ai login",
            },
            {"name": "engineer@a1b2c3-0002", "lane": "eng", "status": "working", "task": "t1", "route": ""},
        ]
    )
    assert [(r["name"], r["route"]) for r in rows] == [
        ("master@a1b2c3-0001", "claude ai login"),
        ("engineer@a1b2c3-0002", ""),
    ]


@pytest.mark.feature("balance-master-route")
def test_the_master_name_cell_names_its_account_and_route() -> None:
    assert (
        'a.account ? h("span", {}, idCell(a.name), h("span", { class: "sw-sub", title: `account ${a.account}`, '
        "text: a.route ? `${a.account} · ${a.route}` : a.account })) : idCell(a.name)" in page_source()
    )
