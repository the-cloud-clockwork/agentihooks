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


def test_each_quota_row_shows_the_balance_state_and_routing_left_even_when_the_tick_decided_otherwise():
    sw = {
        "quota_capacity": DECISION,
        "quota": {
            "cap": 3,
            "rows": [
                {"account": "alpha", "agent": "claude", "state": "DRAIN", "routing_left": 9.6, "sessions": 2},
                {"account": "alpha", "agent": "codex", "state": "CLOSED", "routing_left": 3, "sessions": 0},
                {"account": "beta", "agent": "codex", "state": "SIGNED_OUT", "routing_left": None, "sessions": 0},
                {"account": "gamma", "agent": "claude", "five_hour_left": 70, "seven_day_left": 30, "sessions": 1},
            ],
        },
    }
    out = run_js(
        ["percent", "resetIn", "accountState", "quotaRows"],
        f"quotaRows({json.dumps(sw)}, {NOW}).map((q) => [q.account, q.harness, q.state, q.routing])",
    )
    assert out == [
        ["alpha", "claude", "drain", "10%"],
        ["alpha", "codex", "closed", "3%"],
        ["beta", "codex", "signed out", "—"],
        ["gamma", "claude", "—", "—"],
    ]


def test_an_unknown_routing_reading_shows_a_dash():
    out = run_js(
        ["percent", "accountState"],
        "[accountState({account: 'a', agent: 'claude', state: 'UNKNOWN', routing_left: null})]",
    )
    assert out == [{"state": "unknown", "routing": "—"}]


def test_the_quota_table_heads_state_and_routing():
    page = page_source()
    head = re.search(r'<table class="sw-table" id="quota-table"><thead><tr>(.*?)</tr>', page).group(1)
    assert re.findall(r"<th[^>]*>(.*?)</th>", head) == [
        "account",
        "harness",
        "kind",
        "state",
        "5h left",
        "5h reset",
        "7d left",
        "7d reset",
        "routing",
        "sessions",
        "weight",
        "cap",
        '<span class="sr-only">master</span>',
    ]
    assert 'emptyRow(13, "No quota observed yet.")' in page
    assert 'id="quota-capacity"' not in page


def test_account_states_take_their_role_colour():
    page = page_source()
    assert "#quota-table .lbl.open { color: var(--positive); }" in page
    assert "#quota-table .lbl.closed { color: var(--destructive); }" in page
    assert "#quota-table .lbl.drain { color: var(--warn); }" in page
    assert ".lbl.awaiting-decision, .lbl.unknown { color: var(--warn); }" in page
