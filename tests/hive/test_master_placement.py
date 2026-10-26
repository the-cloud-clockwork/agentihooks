import pytest

from scripts.hive import placement, registry

NOW = 100_000
REPO = "agentihooks"


def hive(name, **fields):
    record = {
        **registry._default(name, "yes"),
        "repos": [REPO],
        "heartbeat_at": NOW,
        "agents": 0,
        "slots": [{"harness": "claude", "account": "token", "kind": "subscription"}],
        **fields,
    }
    return {"room": record["max_agents"] - record["agents"], **record}


def test_master_prefers_the_declared_interactive_login_before_ui_tokens():
    hives = [
        hive("token", prefer={"master": 1}),
        hive("a-second", interactive={"claude": "login"}, prefer={"master": 3}),
        hive("z-first", interactive={"claude": "login"}, prefer={"master": 2}),
        hive("wrong", interactive={"claude": "other"}, prefer={"master": 0}),
    ]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.MasterPlacement(
        "z-first", "login", "interactive", False
    )


@pytest.mark.parametrize("kind", ["subscription", "api"])
def test_master_falls_back_to_a_ui_slot_before_a_preferred_headless_hive(kind):
    hives = [
        hive("headless", ui="no", excluded=[], prefer={"master": 0}),
        hive("ui", slots=[{"harness": "claude", "account": "fallback", "kind": kind}]),
    ]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.MasterPlacement(
        "ui", "fallback", kind, False
    )


@pytest.mark.parametrize(
    ("excluded", "expected"),
    [
        (
            ["master"],
            placement.Wait(f"no hive for master lists the repo {REPO}"),
        ),
        ([], placement.MasterPlacement("headless", "token", "subscription", False)),
    ],
)
def test_master_uses_a_headless_hive_only_when_it_does_not_exclude_the_master(excluded, expected):
    hives = [hive("ui", repos=[]), hive("headless", ui="no", excluded=excluded)]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == expected


def test_master_reconsiders_interactive_hives_at_the_next_handoff():
    hives = [hive("ui"), hive("login", interactive={"claude": "login"}, heartbeat_at=1)]
    assert placement.place_master("claude", "", REPO, hives, NOW).hive == "ui"
    hives[1]["heartbeat_at"] = NOW
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "login", "login", "interactive", False
    )


@pytest.mark.parametrize("fields", [{"heartbeat_at": 1}, {"excluded": ["master"]}])
def test_master_waits_when_no_live_hive_takes_the_master(fields):
    assert placement.place_master("claude", "login", REPO, [hive("bad", **fields)], NOW) == placement.Wait(
        "no live hive takes master"
    )


def test_master_wait_names_the_missing_repo():
    hives = [hive("bad", repos=["other"], interactive={"claude": "login"})]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.Wait(
        f"no hive for master lists the repo {REPO}"
    )


@pytest.mark.parametrize(
    "slots",
    [
        [],
        [{"harness": "codex", "account": "token", "kind": "subscription"}],
        [{"harness": "claude", "account": "token", "kind": "interactive"}],
    ],
)
def test_master_wait_names_the_missing_slot(slots):
    assert placement.place_master("claude", "", REPO, [hive("bad", slots=slots)], NOW) == placement.Wait(
        "no hive for master advertises a claude slot"
    )


@pytest.mark.parametrize(
    ("fields", "front"),
    [({"interactive": {"claude": "other"}}, False), ({"master_login": {"claude": "forced"}}, True)],
)
def test_master_wait_names_the_missing_login(fields, front):
    hives = [hive("bad", slots=[], **fields)]
    assert placement.place_master("claude", "login", REPO, hives, NOW, front) == placement.Wait(
        "no hive for master holds the claude login for login"
    )


def test_master_ties_follow_hive_preference_room_lifetime_and_slot_name():
    hives = [
        hive("ephemeral", ephemeral="yes", max_agents=4),
        hive("small", max_agents=2),
        hive("full", max_agents=5, agents=5),
        hive(
            "room",
            max_agents=4,
            slots=[
                {"harness": "claude", "account": "z", "kind": "subscription"},
                {"harness": "claude", "account": "a", "kind": "api"},
            ],
        ),
    ]
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "room", "a", "api", False
    )
    hives[2]["prefer"] = {"master": 0}
    assert placement.place_master("claude", "", REPO, hives, NOW).hive == "full"


def test_master_skips_ineligible_hives_and_slots_without_ending_the_search():
    hives = [
        hive("wrong-role", excluded=["master"]),
        hive(
            "valid",
            slots=[
                {"harness": "codex", "account": "wrong", "kind": "subscription"},
                {"harness": "claude", "account": "native", "kind": "interactive"},
                {"harness": "claude", "account": "token", "kind": "subscription"},
            ],
        ),
    ]
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "valid", "token", "subscription", False
    )


def test_duplicate_account_labels_keep_the_first_route_without_comparing_placement_objects():
    hives = [
        hive(
            "ui",
            slots=[
                {"harness": "claude", "account": "shared", "kind": "subscription"},
                {"harness": "claude", "account": "shared", "kind": "api"},
            ],
        )
    ]
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "ui", "shared", "subscription", False
    )


def test_the_front_door_master_takes_a_hive_master_login_and_never_its_slots():
    hives = [hive("login", master_login={"claude": "operator"}, prefer={"master": 1}), hive("other")]
    assert placement.place_master("claude", "", REPO, hives, NOW, front=True) == placement.MasterPlacement(
        "login", "operator", "master-login", False
    )
    hives[0]["master_login"] = {"claude": ""}
    assert placement.place_master("claude", "", REPO, hives, NOW, front=True) == placement.MasterPlacement(
        "login", "token", "subscription", False
    )


def test_a_secondary_master_ignores_a_hive_master_login():
    hives = [hive("login", master_login={"claude": "operator"})]
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "login", "token", "subscription", False
    )


def test_a_declared_login_skips_a_hive_whose_master_login_differs():
    hives = [hive("login", master_login={"claude": "operator"}), hive("native", interactive={"claude": "declared"})]
    assert placement.place_master("claude", "declared", REPO, hives, NOW, front=True) == placement.MasterPlacement(
        "native", "declared", "interactive", False
    )


def test_a_declared_login_matching_the_hive_master_login_is_placed_there():
    hives = [hive("login", master_login={"claude": "operator"})]
    assert placement.place_master("claude", "operator", REPO, hives, NOW, front=True) == placement.MasterPlacement(
        "login", "operator", "master-login", False
    )


def test_a_master_login_outranks_a_preferred_hive_slot_and_follows_master_preference():
    hives = [
        hive("login", master_login={"claude": "operator"}, prefer={"master": 2}),
        hive("slot", prefer={"master": 1}),
    ]
    assert placement.place_master("claude", "", REPO, hives, NOW, front=True).hive == "login"
    hives = [
        hive("a", master_login={"claude": "first"}, prefer={"master": 2}),
        hive("b", master_login={"claude": "second"}, prefer={"master": 1}),
    ]
    assert placement.place_master("claude", "", REPO, hives, NOW, front=True) == placement.MasterPlacement(
        "b", "second", "master-login", False
    )
