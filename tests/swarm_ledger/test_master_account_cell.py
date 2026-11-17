import pytest

from tests.swarm_ledger.ledger_page import chromium, ledger_state, page_source, shell_html, show
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
@pytest.mark.parametrize(
    ("account", "lines"),
    [
        ("nestor_coltatgmail_com", ["nestor_coltatgmail_com", "claude ai login", "root"]),
        ("", ["claude ai login", "root"]),
    ],
)
def test_the_master_row_shows_its_route_on_its_own_line_at_1920(account: str, lines: list[str]) -> None:
    master = {
        "name": "master@a1b2c3-0001",
        "lane": "master",
        "status": "working",
        "account": account,
        "route": "claude ai login",
        "hive": "root",
    }
    swarm = {
        "config": {"slug": "sw", "state": "running", "max_eng": 1, "max_ci": 1},
        "tasks": {},
        "agents": [master],
        "findings": [],
        "handoffs": [],
    }
    doc = {"title": "Route", "overview": "o", "tasks": [{"id": "t1", "title": "One", "lane": "eng", "state": "open"}]}
    with chromium() as browser:
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        show(page, shell_html(), ledger=ledger_state(doc), swarm=swarm)
        page.get_by_role("tab", name="Swarm", exact=False).click()
        row = page.locator("#swarm-agents tr").first
        shown = row.locator(".sw-sub").all_inner_texts()
        clipped = row.locator(".sw-sub").evaluate_all("subs => subs.map((sub) => sub.scrollWidth > sub.clientWidth)")
        page.close()
    assert shown == lines
    assert clipped[lines.index("claude ai login")] is False


@pytest.mark.feature("balance-master-route")
def test_the_master_name_cell_names_its_account_and_route() -> None:
    source = page_source()
    assert "(nameCell(a)), a.lane," in source
    assert (
        'const subs = [a.account && h("span", { class: "sw-sub", title: `account ${a.account}`, text: a.account }), '
        'a.route && h("span", { class: "sw-sub", text: a.route })].filter(Boolean);' in source
    )
