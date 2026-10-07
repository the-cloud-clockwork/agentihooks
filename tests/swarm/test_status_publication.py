import pytest

from scripts.swarm import status
from tests.swarm.test_commands import store

pytestmark = pytest.mark.xdist_group("fakeredis")
__all__ = ["store"]


def test_status_exposes_exact_invalid_plan_diagnostics(store):
    report = status.status_report(store, "sw", {"tasks": [{"id": "t", "depends_on": ["missing"]}]})
    assert report["plan_shape"] == {"error": "plan has unknown dependencies: missing"}


def test_empty_status_has_a_valid_plan_shape(store):
    assert "error" not in status.status_report(store, "sw", {})["plan_shape"]
