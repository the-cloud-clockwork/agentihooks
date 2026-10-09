from scripts.swarm import quota_view

MINUTE = 60_000


def row(name, state="OPEN", sessions=0, five=80.0, week=60.0, harness="claude"):
    return {
        "harness": harness,
        "name": name,
        "state": state,
        "sessions": sessions,
        "five_left": five,
        "week_left": week,
    }


DECISION = {
    "configured": {"eng": 4, "ci": 1, "plan": 1},
    "effective": {"eng": 2, "ci": 1, "plan": 0},
    "placeable": {"claude": 1, "codex": 0},
    "reason": "accounts are closed; Claude has 1 free seats and Codex has 0 free seats",
    "accounts": [
        row("alpha", "CLOSED", 2, 40.0, 7.5),
        row("beta", sessions=1),
        row("main", "UNKNOWN", 0, None, None, "codex"),
    ],
    "at": 1_000_000,
}


def test_status_lines_name_each_lane_cap_the_reason_the_change_age_and_every_account():
    assert quota_view.lines(DECISION, 1_000_000 + 5 * MINUTE) == [
        "quota capacity eng 2 of 4, ci 1 of 1, plan 0 of 1, changed 5 minutes ago, because accounts are closed; "
        "Claude has 1 free seats and Codex has 0 free seats",
        "quota account claude alpha  closed  routing 7.5% left  sessions 2",
        "quota account claude beta  open  routing 60% left  sessions 1",
        "quota account codex main  unknown  routing unknown  sessions 0",
    ]


def test_a_change_under_a_minute_old_reads_just_now_and_one_minute_is_singular():
    assert quota_view.lines({**DECISION, "accounts": []}, 1_000_000 + 59_999)[0].split(", ")[3] == "changed just now"
    assert (
        quota_view.lines({**DECISION, "accounts": []}, 1_000_000 + MINUTE)[0].split(", ")[3] == "changed 1 minute ago"
    )


def test_no_decision_says_capacity_was_not_observed():
    assert quota_view.lines({}, 0) == ["quota capacity has not been observed"]


def test_routing_left_is_the_lower_window_or_none_when_either_is_unknown():
    assert quota_view.routing_left(row("a", five=30.0, week=70.0)) == 30.0
    assert quota_view.routing_left(row("a", five=90.0, week=12.0)) == 12.0
    assert quota_view.routing_left(row("a", five=None, week=12.0)) is None
    assert quota_view.routing_left(row("a", five=12.0, week=None)) is None


def test_the_page_reads_each_account_routing_and_the_lane_order():
    assert quota_view.page({}) == {}
    assert quota_view.page(DECISION) == {
        **DECISION,
        "lanes": ["eng", "ci", "plan"],
        "accounts": [
            {**row("alpha", "CLOSED", 2, 40.0, 7.5), "routing": 7.5},
            {**row("beta", sessions=1), "routing": 60.0},
            {**row("main", "UNKNOWN", 0, None, None, "codex"), "routing": None},
        ],
    }


def api_row(harness="claude", sessions=1, weight=25, state="OPEN"):
    return {**row("api", state, sessions, None, None, harness), "kind": "api", "weight": weight}


API_DECISION = {
    **DECISION,
    "accounts": [row("alpha", sessions=2), row("beta", sessions=1), api_row(), api_row("codex", 0, None, "CLOSED")],
}


def test_each_api_line_shows_its_harness_share_against_its_weight():
    assert quota_view.lines(API_DECISION, 1_000_000)[3:] == [
        "quota account claude api  open  api share 25% of 4 sessions against weight 25%  sessions 1",
        "quota account codex api  closed  api share 0% of 0 sessions against no weight  sessions 0",
    ]


def test_the_page_carries_each_api_share():
    accounts = quota_view.page(API_DECISION)["accounts"]
    assert [account.get("share") for account in accounts] == [None, None, 25, 0]


def test_the_api_share_rounds_the_api_percent_of_its_harness_sessions():
    rows = [row("a", sessions=1), api_row(sessions=3), row("cx", sessions=5, harness="codex")]
    assert quota_view.api_share(rows[1], rows) == (75, 4)
