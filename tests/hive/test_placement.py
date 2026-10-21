import pytest

from scripts.hive import registry
from scripts.hive.placement import Wait, place

NOW = 10_000_000
REPO = "agentihooks"


def hive(hive_id: str, **fields) -> dict:
    record = {
        **registry._default(hive_id, "yes"),
        "max_agents": 2,
        "slots": [{"harness": "claude", "account": "acct", "kind": "subscription", "cap": 4}],
        "interactive": {"claude": "acct", "codex": ""},
        "repos": [REPO],
        "heartbeat_at": NOW - registry.LIVE_MS,
        "agents": 0,
        **fields,
    }
    return {"room": record["max_agents"] - record["agents"], **record}


def fleet(*off: str, **changes: dict) -> list[dict]:
    hives = [
        hive("pc", excluded=["qa", "frontend", "eng", "ci", "plan"], prefer={"master": 1}, max_agents=1),
        hive("vm", excluded=["eng", "ci", "plan"], prefer={"master": 2, "qa": 1, "frontend": 1}),
        hive("pods", ui="no", ephemeral="yes", excluded=["master", "qa", "frontend"], max_agents=4),
    ]
    return [
        {
            **record,
            **changes.get(record["id"], {}),
            **({"heartbeat_at": NOW - registry.LIVE_MS - 1} if record["id"] in off else {}),
        }
        for record in hives
    ]


CASES = {
    "all live master": ("master", fleet(), "subscription", REPO, "pc"),
    "all live qa": ("qa", fleet(), "subscription", REPO, "vm"),
    "all live frontend": ("frontend", fleet(), "subscription", REPO, "vm"),
    "all live eng": ("eng", fleet(), "subscription", REPO, "pods"),
    "all live ci": ("ci", fleet(), "subscription", REPO, "pods"),
    "all live plan": ("plan", fleet(), "subscription", REPO, "pods"),
    "pc off master": ("master", fleet("pc"), "subscription", REPO, "vm"),
    "pc off qa": ("qa", fleet("pc"), "subscription", REPO, "vm"),
    "both off master": ("master", fleet("pc", "vm"), "subscription", REPO, Wait("no live hive takes master")),
    "headless hive that allows the master": (
        "master",
        fleet("pc", "vm", pods={"excluded": ["qa", "frontend"]}),
        "subscription",
        REPO,
        "pods",
    ),
    "unfit UI master hive beside a headless hive that allows the master": (
        "master",
        fleet("pc", vm={"repos": []}, pods={"excluded": ["qa", "frontend"]}),
        "subscription",
        REPO,
        "pods",
    ),
    "both off qa": ("qa", fleet("pc", "vm"), "subscription", REPO, Wait("no live hive takes qa")),
    "both off frontend": ("frontend", fleet("pc", "vm"), "subscription", REPO, Wait("no live hive takes frontend")),
    "both off eng": ("eng", fleet("pc", "vm"), "subscription", REPO, "pods"),
    "all off master": ("master", fleet("pc", "vm", "pods"), "subscription", REPO, Wait("no live hive takes master")),
    "pods off eng": ("eng", fleet("pods"), "subscription", REPO, Wait("no live hive takes eng")),
    "all off eng": ("eng", fleet("pc", "vm", "pods"), "subscription", REPO, Wait("no live hive takes eng")),
    "master beside the only live hive, a qa hive": (
        "master",
        fleet("pc", "pods", vm={"excluded": ["master", "eng", "ci", "plan"]}),
        "subscription",
        REPO,
        Wait("no live hive takes master"),
    ),
    "vm off qa beside a live pc": ("qa", fleet("vm"), "subscription", REPO, Wait("no live hive takes qa")),
    "master beside a live qa hive": (
        "master",
        fleet("pc", vm={"excluded": ["master", "eng", "ci", "plan"]}),
        "subscription",
        REPO,
        Wait("no live hive takes master"),
    ),
    "full pc keeps the master": ("master", fleet(pc={"agents": 1}), "subscription", REPO, "pc"),
    "full vm qa": ("qa", fleet(vm={"agents": 2}), "subscription", REPO, Wait("no hive for qa has room")),
    "vm one short qa": ("qa", fleet(vm={"agents": 1}), "subscription", REPO, "vm"),
    "full pods eng": ("eng", fleet(pods={"agents": 4}), "subscription", REPO, Wait("no hive for eng has room")),
    "repo missing eng": ("eng", fleet(), "subscription", "other", Wait("no hive for eng lists the repo other")),
    "repo missing on pc": ("master", fleet(pc={"repos": ["other"]}), "subscription", REPO, "vm"),
    "slot missing qa": (
        "qa",
        fleet(vm={"slots": [{"harness": "codex", "account": "acct"}, {"harness": "claude", "account": "other"}]}),
        "subscription",
        REPO,
        Wait("no hive for qa advertises the claude slot acct"),
    ),
    "slot among others": (
        "qa",
        fleet(vm={"slots": [{"harness": "claude", "account": "acct"}, {"harness": "codex", "account": "acct"}]}),
        "subscription",
        REPO,
        "vm",
    ),
    "slot missing on pc": ("master", fleet(pc={"slots": []}), "subscription", REPO, "vm"),
    "login missing master": (
        "master",
        fleet(pc={"interactive": {"claude": ""}}, vm={"interactive": {"claude": "other"}}),
        "interactive",
        REPO,
        Wait("no hive for master holds the claude login for acct"),
    ),
    "login missing on pc": ("master", fleet(pc={"interactive": {}}), "interactive", REPO, "vm"),
    "login present": ("master", fleet(), "interactive", REPO, "pc"),
    "login ignored for subscription": ("master", fleet(pc={"interactive": {}}), "subscription", REPO, "pc"),
    "master login missing": (
        "master",
        fleet(vm={"master_login": {"claude": "other"}}),
        "master-login",
        REPO,
        Wait("no hive for master holds the claude master login for acct"),
    ),
    "master login on vm": ("master", fleet(vm={"master_login": {"claude": "acct"}}), "master-login", REPO, "vm"),
}


@pytest.mark.parametrize(("role", "hives", "route_kind", "repo", "expected"), CASES.values(), ids=CASES.keys())
def test_each_operator_example_case_places_or_waits(role, hives, route_kind, repo, expected):
    assert place(role, "claude", "acct", route_kind, repo, hives, NOW) == expected


def eng(hive_id: str, **fields) -> dict:
    return hive(hive_id, ui="no", excluded=["master"], max_agents=4, **fields)


ORDER = {
    "rank first": ([eng("a", agents=3), eng("b", prefer={"eng": 2}), eng("c", prefer={"eng": 1}, agents=3)], "c"),
    "unranked last": ([eng("a"), eng("b", prefer={"eng": 5}, agents=3)], "b"),
    "most free room": ([eng("a", agents=2), eng("b", agents=1), eng("c", agents=3)], "b"),
    "durable before ephemeral": ([eng("a", ephemeral="yes"), eng("b")], "b"),
    "id breaks the tie": ([eng("b"), eng("a")], "a"),
}


@pytest.mark.parametrize(("hives", "expected"), ORDER.values(), ids=ORDER.keys())
def test_the_order(hives, expected):
    assert place("eng", "claude", "acct", "subscription", REPO, hives, NOW) == expected


def test_the_master_never_lands_on_a_hive_that_excludes_it():
    hives = [eng("a", agents=4), eng("b", agents=5), hive("vm", heartbeat_at=0)]
    assert place("master", "claude", "acct", "subscription", REPO, hives, NOW) == Wait("no live hive takes master")
    assert place("qa", "claude", "acct", "subscription", REPO, hives, NOW) == Wait("no live UI hive takes qa")


def test_a_full_master_ui_hive_keeps_the_master_without_counting_against_it():
    hives = [hive("vm", agents=2), eng("a")]
    assert place("master", "claude", "acct", "subscription", REPO, hives, NOW) == "vm"


def test_the_codex_harness_matches_its_own_slot_and_login():
    codex = {"harness": "codex", "account": "acct"}
    hives = [
        eng("a", slots=[codex], interactive={"codex": ""}),
        eng("b", slots=[codex], interactive={"codex": "acct"}, agents=3),
        eng("c", interactive={"claude": "acct", "codex": "acct"}),
    ]
    assert place("eng", "codex", "acct", "interactive", REPO, hives, NOW) == "b"
    assert place("eng", "codex", "acct", "subscription", REPO, hives, NOW) == "a"
