from scripts.swarm import naming, templates
from scripts.swarm.store import SwarmConfig


def test_template_accepts_the_planner_lane_with_its_default_cap():
    template = templates.parse({"name": "planner-proof", "lanes": {"plan": {}}})
    assert template.lanes["plan"].cap == 1


def test_planner_lane_defaults_to_the_planner_profile():
    template = templates.parse({"name": "planner-proof"})
    assert templates.lane_map(template)["plan"]["profile"] == "planner"


def test_planner_names_round_trip_to_the_plan_lane():
    name = naming.build("planner", "abcdef", 1)
    assert name == "planner@abcdef-0001"
    assert naming.parse(name).lane == "plan"
    assert naming.lane_of(name) == "plan"


def test_existing_swarm_configs_default_to_one_planner():
    assert SwarmConfig("planner-proof", "/repo", 2, 1).max_plan == 1
