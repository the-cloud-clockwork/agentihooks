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
        "slots": [{"harness": "claude", "account": "token", "kind": "subscription"}],
        **fields,
    }


def test_master_prefers_the_declared_interactive_login_before_ui_tokens():
    hives = [
        hive("token", prefer={"master": 1}),
        hive("second", interactive={"claude": "login"}, prefer={"master": 3}),
        hive("first", interactive={"claude": "login"}, prefer={"master": 2}),
        hive("wrong", interactive={"claude": "other"}, prefer={"master": 0}),
    ]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.MasterPlacement(
        "first", "login", "interactive", False
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


def test_master_uses_headless_when_ui_hives_cannot_run_it():
    hives = [hive("ui", repos=[]), hive("headless", ui="no", roles=[])]
    assert placement.place_master("claude", "login", REPO, hives, NOW) == placement.MasterPlacement(
        "headless", "token", "subscription", True
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
