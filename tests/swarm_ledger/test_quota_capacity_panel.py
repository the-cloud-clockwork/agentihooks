import json
import re
import subprocess

from tests.swarm_ledger.ledger_page import page_source

NOW = 1_800_000_000_000
DECISION = {
    "configured": {"eng": 4, "ci": 1, "plan": 1},
    "effective": {"eng": 2, "ci": 1, "plan": 0},
    "reason": "accounts are closed; Claude has 1 free seats and Codex has 0 free seats",
    "lanes": ["eng", "ci", "plan"],
    "accounts": [
        {
            "harness": "claude",
            "name": "alpha",
            "state": "CLOSED",
            "sessions": 2,
            "five_left": 40.4,
            "week_left": 7.6,
            "routing": 7.6,
        },
        {
            "harness": "codex",
            "name": "alpha",
            "state": "OPEN",
            "sessions": 0,
            "five_left": 90,
            "week_left": 80,
            "routing": 80,
        },
    ],
    "at": NOW - 5 * 60_000,
}


def function_source(name):
    return f"function {name}(" + page_source().split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def run_js(names, expr):
    script = "".join(function_source(n) + "\n" for n in names) + f"process.stdout.write(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def span(ms):
    return f"{round(ms / 60000)}m"


def test_each_quota_row_carries_the_state_and_routing_left_the_tick_decided_on_and_dashes_without_one():
    sw = {
        "quota_capacity": DECISION,
        "quota": {
            "cap": 3,
            "rows": [
                {"account": "alpha", "agent": "claude", "five_hour_left": 41, "seven_day_left": 9, "sessions": 2},
                {"account": "alpha", "agent": "codex", "five_hour_left": 90, "seven_day_left": 80, "sessions": 0},
                {"account": "gamma", "agent": "claude", "five_hour_left": 70, "seven_day_left": 30, "sessions": 1},
            ],
        },
    }
    out = run_js(
        ["percent", "resetIn", "accountState", "quotaRows"],
        f"quotaRows({json.dumps(sw)}, {NOW}).map((q) => [q.account, q.harness, q.state, q.routing])",
    )
    assert out == [
        ["alpha", "claude", "closed", "8%"],
        ["alpha", "codex", "open", "80%"],
        ["gamma", "claude", "—", "—"],
    ]


def test_routing_left_is_unknown_when_a_window_is_unknown():
    out = run_js(
        ["percent", "accountState"],
        "[accountState({quota_capacity: {accounts: [{harness: 'claude', name: 'a', state: 'UNKNOWN', routing: null}]}},"
        " {account: 'a', agent: 'claude', five_hour_left: 50, seven_day_left: 40})]",
    )
    assert out == [{"state": "unknown", "routing": "—"}]


def test_the_capacity_line_reads_each_lane_against_its_cap_the_change_age_and_the_reason():
    out = run_js(
        ["span", "capacityLine"],
        f"[capacityLine({json.dumps(DECISION)}, {NOW}), capacityLine({{}}, {NOW}), capacityLine(undefined, {NOW})]",
    )
    assert out == [
        {
            "lanes": "eng 2 of 4 · ci 1 of 1 · plan 0 of 1",
            "changed": "changed 5m ago",
            "reason": "because accounts are closed; Claude has 1 free seats and Codex has 0 free seats",
        },
        None,
        None,
    ]


def test_the_quota_table_heads_state_and_routing_and_the_box_holds_the_capacity_line():
    page = page_source()
    head = re.search(r'<table class="sw-table" id="quota-table"><thead><tr>(.*?)</tr>', page).group(1)
    assert re.findall(r"<th[^>]*>(.*?)</th>", head) == [
        "account",
        "harness",
        "state",
        "5h left",
        "5h reset",
        "7d left",
        "7d reset",
        "routing",
        "sessions",
        '<span class="sr-only">master</span>',
    ]
    box = page.split('id="quota-box"', 1)[1].split('id="doctor-box"', 1)[0]
    assert 'id="quota-capacity"' in box
    assert 'emptyRow(10, "No quota observed yet. Run agentihooks balance.")' in page


def test_account_states_take_their_role_colour():
    page = page_source()
    assert "#quota-table .lbl.open { color: var(--positive); }" in page
    assert "#quota-table .lbl.closed { color: var(--destructive); }" in page
    assert ".lbl.awaiting-decision, .lbl.unknown { color: var(--warn); }" in page
