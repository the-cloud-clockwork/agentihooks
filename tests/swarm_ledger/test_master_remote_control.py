import json
import subprocess

from tests.swarm_ledger.ledger_page import page_source

STUBS = """
const h = (tag, attrs, ...kids) => ({tag, attrs, kids});
const label = (text, tone) => ({label: text, tone});
const cells = (...values) => values;
const agentCellTips = (row) => row;
const withId = (row) => row;
const idCell = (text) => text;
const transportCell = (c) => c ? "transport" : "—";
const agentActions = () => "actions";
const pending = "", swarmReadError = false;
"""


def _function(page, name):
    return "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def _run(script):
    page = page_source()
    names = ("span", "modelText", "terminalView", "executionView", "agentRows", "agentRow", "joinCell")
    js = STUBS + "\n".join(_function(page, name) for name in names) + script
    return json.loads(subprocess.check_output(["node", "-e", js], text=True))


def test_only_a_master_with_a_recorded_decision_gets_the_remote_control_cell():
    cells = _run("""
const on = {state: 'on', detail: 'master 1 · sw'};
const raw = {agents: [
  {name: 'm', lane: 'master', remote_control: on, capabilities: {push_inbox: true}},
  {name: 'e', lane: 'eng', remote_control: on},
  {name: 'q', lane: 'master', remote_control: {}}
]};
console.log(JSON.stringify(agentRows(raw, 0).map((r) => agentRow(r, false)[5])));
""")
    assert cells == [
        {
            "tag": "span",
            "attrs": {"data-tip": "Remote Control: on (master 1 · sw), phone access available"},
            "kids": [
                {"label": "phone access available", "tone": "on"},
                {"tag": "span", "attrs": {"class": "sw-sub", "text": "master 1 · sw"}, "kids": []},
                "transport",
            ],
        },
        "—",
        "—",
    ]


def test_an_off_master_without_transport_shows_phone_access_unavailable_with_the_reason():
    cell = _run("""
const raw = {agents: [{name: 'm', lane: 'master', remote_control: {state: 'off', detail: 'fallback auth'}}]};
console.log(JSON.stringify(agentRow(agentRows(raw, 0)[0], false)[5]));
""")
    assert cell == {
        "tag": "span",
        "attrs": {"data-tip": "Remote Control: off (fallback auth), phone access unavailable"},
        "kids": [
            {"label": "phone access unavailable", "tone": "off"},
            {
                "tag": "span",
                "attrs": {"class": "sw-sub", "text": "fallback auth"},
                "kids": [],
            },
        ],
    }


def test_a_master_on_the_token_fallback_card_says_phone_access_unavailable():
    cell = _run("""
const off = {state: 'off', detail: 'subscription token fallback'};
const raw = {agents: [{name: 'm', lane: 'master', remote_control: off}]};
console.log(JSON.stringify(agentRow(agentRows(raw, 0)[0], false)[5]));
""")
    assert cell["attrs"] == {"data-tip": "Remote Control: off (subscription token fallback), phone access unavailable"}
    assert cell["kids"][0] == {"label": "phone access unavailable", "tone": "off"}
    assert cell["kids"][1]["attrs"]["text"] == "subscription token fallback"
