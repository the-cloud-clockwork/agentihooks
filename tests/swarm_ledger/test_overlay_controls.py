import json
import re
from pathlib import Path

import pytest

from hooks.context.profile_chain import OVERLAY_CAP
from scripts.swarm import overlays
from scripts.swarm_ledger import ledger_server

SWARM_JS = Path(ledger_server.__file__).parent / "static" / "js" / "swarm.js"


def test_the_page_offers_the_server_roles_and_overlay_cap():
    source = SWARM_JS.read_text()
    roles = re.search(r"^const ROLES = (\[.*\]);$", source, re.M).group(1)
    cap = re.search(r"^const OVERLAY_CAP = (\d+);$", source, re.M).group(1)
    assert tuple(json.loads(roles)) == overlays.ROLES
    assert int(cap) == OVERLAY_CAP


def test_page_sets_overlays_per_role_in_role_order():
    body = {"action": "set", "overlays": {"planner": [], "engineer": ["qitp-tuner", "trader", "reviewer"]}}
    assert ledger_server.control_argv(body) == [
        "set",
        "overlays-engineer=qitp-tuner,trader,reviewer",
        "overlays-planner=",
    ]


def test_page_sets_overlays_beside_other_settings_in_one_change():
    body = {"action": "set", "autonomy": "full", "overlays": {"qa": ["qitp-tuner", "trader", "reviewer"]}}
    assert ledger_server.control_argv(body) == ["set", "autonomy=full", "overlays-qa=qitp-tuner,trader,reviewer"]


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
    body = {"action": "set", "overlays": {"master": ["a.b_c-d", "Z9", "x_y"]}}
    assert ledger_server.control_argv(body) == ["set", "overlays-master=a.b_c-d,Z9,x_y"]
