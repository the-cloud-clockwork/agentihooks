import pytest

from scripts.hive import placement, registry

NOW = 100_000
REPO = "agentihooks"


def hive(name, **fields):
    return {
        **registry._default(name),
        "roles": ["master"],
        "ui": "yes",
        "repos": [REPO],
        "heartbeat_at": NOW,
        "agents": 0,
        "slots": [{"harness": "claude", "account": "token", "kind": "subscription"}],
        **fields,
    }


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
        hive("headless", ui="no", prefer={"master": 0}),
        hive("ui", slots=[{"harness": "claude", "account": "fallback", "kind": kind}]),
    ]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.MasterPlacement(
        "ui", "fallback", kind, False
    )


@pytest.mark.parametrize("roles", [[], ["master"]])
def test_master_never_uses_a_headless_hive_when_ui_hives_cannot_run_it(roles):
    hives = [hive("ui", repos=[]), hive("headless", ui="no", roles=roles)]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.Wait(
        "no live hive can run the master with interactive, subscription or api authentication"
    )


def test_master_reconsiders_interactive_hives_at_the_next_handoff():
    hives = [hive("ui"), hive("login", interactive={"claude": "login"}, heartbeat_at=1)]
    assert placement.place_master("claude", "", REPO, hives, NOW).hive == "ui"
    hives[1]["heartbeat_at"] = NOW
    assert placement.place_master("claude", "", REPO, hives, NOW) == placement.MasterPlacement(
        "login", "login", "interactive", False
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"heartbeat_at": 1},
        {"repos": []},
        {"roles": ["eng"]},
        {"slots": []},
        {"slots": [{"harness": "codex", "account": "token", "kind": "subscription"}]},
        {"slots": [{"harness": "claude", "account": "token", "kind": "interactive"}]},
    ],
)
def test_master_waits_when_no_hive_has_an_eligible_authentication_route(fields):
    assert placement.place_master("claude", "login", REPO, [hive("bad", **fields)], NOW) == placement.Wait(
        "no live hive can run the master with interactive, subscription or api authentication"
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
        hive("wrong-role", roles=["eng"]),
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
