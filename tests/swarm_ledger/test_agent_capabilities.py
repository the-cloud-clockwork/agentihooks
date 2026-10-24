import json
import re
import subprocess

from tests.swarm_ledger.ledger_page import page_source


def _function(page, name):
    return "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def test_agent_rows_carry_push_inbox_and_native_terminal_per_agent():
    page = page_source()
    names = ("span", "modelText", "terminalView", "executionView", "agentRows")
    js = (
        "\n".join(_function(page, name) for name in names)
        + """
const raw = {agents: [
  {name: 'c', lane: 'eng', harness: 'claude', capabilities: {push_inbox: true, native_terminal_attach: false}},
  {name: 'p', lane: 'eng', harness: 'codex', capabilities: {push_inbox: true, native_terminal_attach: true}},
  {name: 'l', lane: 'eng', harness: 'codex', capabilities: {push_inbox: false, native_terminal_attach: false}},
  {name: 'old', lane: 'ci'}
]};
console.log(JSON.stringify(agentRows(raw, 0).map((r) => [r.name, r.transport])));
"""
    )
    rows = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert rows == [
        ["c", {"push_inbox": True, "native_terminal_attach": False}],
        ["p", {"push_inbox": True, "native_terminal_attach": True}],
        ["l", {"push_inbox": False, "native_terminal_attach": False}],
        ["old", None],
    ]


def test_transport_cell_names_both_capabilities_in_plain_labels():
    page = page_source()
    body = page.split("  const transportCell = (c) =>", 1)[1].split(' : "—";', 1)[0]
    for text in ("push", "no push", "native terminal", "no native terminal"):
        assert f'"{text}"' in body
    assert "transportCell(a.transport)" in page


def test_agents_table_has_a_transport_column_with_a_set_width():
    page = page_source()
    head = re.search(r'<table class="sw-table" id="agents-table"><thead><tr>(.*?)</tr>', page).group(1)
    heads = re.findall(r'<th class="ag-(\w+)"', head)
    assert heads[heads.index("model") + 1] == "transport"
    assert re.search(r"#agents-table \.ag-transport \{ width: \d+%; \}", page)
