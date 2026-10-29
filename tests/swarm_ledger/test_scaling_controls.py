import pytest

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api import admin


def test_page_sets_the_scaling_settings_in_one_change():
    body = {"action": "set", "scaling": "manual", "load_high": 2.5, "load_low": 0.5, "memory_per_agent_mb": 900}
    assert ledger_server.control_argv(body) == [
        "set",
        "scaling=manual",
        "load-high=2.5",
        "load-low=0.5",
        "memory-per-agent=900",
    ]


@pytest.mark.parametrize(
    ("body", "pairs"),
    [
        ({"load_high": 2}, ["load-high=2"]),
        ({"load_high": 0.5}, ["load-high=0.5"]),
        ({"load_high": 10, "load_low": 10}, ["load-high=10", "load-low=10"]),
        ({"memory_per_agent_mb": 1}, ["memory-per-agent=1"]),
    ],
)
def test_page_accepts_watermark_and_memory_boundaries(body, pairs):
    assert ledger_server.control_argv({"action": "set", **body}) == ["set", *pairs]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"scaling": "sometimes"}, "scaling must be one of auto, manual"),
        ({"load_high": 1.0, "load_low": 2.0}, "load_low must be at most load_high"),
        ({"load_high": True}, "load_high must be a number above 0 and at most 10"),
        ({"load_low": 0}, "load_low must be a number above 0 and at most 10"),
        ({"load_high": "1.5"}, "load_high must be a number above 0 and at most 10"),
        ({"memory_per_agent_mb": 0}, "memory_per_agent_mb must be a whole number of MB above 0"),
        ({"memory_per_agent_mb": 1.5}, "memory_per_agent_mb must be a whole number of MB above 0"),
        ({"memory_per_agent_mb": True}, "memory_per_agent_mb must be a whole number of MB above 0"),
    ],
)
def test_page_refuses_bad_scaling_settings(body, reason):
    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", **body})
    assert str(caught.value) == reason


def test_the_admin_schema_lists_the_scaling_settings():
    properties = admin.CONTROL["properties"]
    assert properties["scaling"] == {"enum": ["auto", "manual"]}
    assert properties["load_high"] == properties["load_low"] == {"type": "number"}
    assert properties["memory_per_agent_mb"] == {"type": "integer"}
