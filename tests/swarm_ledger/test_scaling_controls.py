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


def test_page_sets_a_whole_number_watermark():
    assert ledger_server.control_argv({"action": "set", "load_high": 2}) == ["set", "load-high=2"]


@pytest.mark.parametrize(
    "body",
    [
        {"scaling": "sometimes"},
        {"load_high": 1.0, "load_low": 2.0},
        {"load_high": True},
        {"load_low": 0},
        {"load_high": "1.5"},
        {"memory_per_agent_mb": 0},
        {"memory_per_agent_mb": 1.5},
    ],
)
def test_page_refuses_bad_scaling_settings(body):
    with pytest.raises(ValueError):
        ledger_server.control_argv({"action": "set", **body})


def test_the_admin_schema_lists_the_scaling_settings():
    properties = admin.CONTROL["properties"]
    assert properties["scaling"] == {"enum": ["auto", "manual"]}
    assert properties["load_high"] == properties["load_low"] == {"type": "number"}
    assert properties["memory_per_agent_mb"] == {"type": "integer"}
