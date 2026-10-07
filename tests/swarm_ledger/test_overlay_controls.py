import pytest

from scripts.swarm_ledger import ledger_server


def test_page_sets_overlays_per_role_in_role_order():
    body = {"action": "set", "overlays": {"planner": [], "engineer": ["qitp-tuner", "trader"]}}
    assert ledger_server.control_argv(body) == ["set", "overlays-engineer=qitp-tuner,trader", "overlays-planner="]


def test_page_sets_at_most_three_overlays_on_a_role():
    names = ["a", "b", "c"]
    assert ledger_server.control_argv({"action": "set", "overlays": {"qa": names}}) == ["set", "overlays-qa=a,b,c"]
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", "overlays": {"qa": [*names, "d"]}})
    assert str(caught.value) == "a role wears at most 3 overlays; qa was given 4"


@pytest.mark.parametrize(
    "overlays",
    [
        {"pilot": ["a"]},
        {},
        [],
        {"engineer": "a"},
        {"engineer": ["-a"]},
        {"engineer": ["a,b"]},
        {"engineer": ["a", "a"]},
        {"engineer": [1]},
        {"engineer": [""]},
    ],
)
def test_page_overlays_need_known_roles_and_plain_unique_names(overlays):
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", "overlays": overlays})
    assert str(caught.value) == (
        "overlays maps a base role of master, engineer, planner, qa, cicd to a list of distinct overlay names"
    )


def test_a_long_overlay_name_is_refused_and_a_long_enough_one_passes():
    longest = "a" * 100
    assert ledger_server.control_argv({"action": "set", "overlays": {"cicd": [longest]}}) == [
        "set",
        f"overlays-cicd={longest}",
    ]
    with pytest.raises(ValueError):
        ledger_server.control_argv({"action": "set", "overlays": {"cicd": [longest + "a"]}})


def test_overlay_names_may_carry_dots_dashes_and_underscores():
    body = {"action": "set", "overlays": {"master": ["a.b_c-d", "Z9"]}}
    assert ledger_server.control_argv(body) == ["set", "overlays-master=a.b_c-d,Z9"]
