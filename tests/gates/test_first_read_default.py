import pytest

from scripts.gates import catalog


@pytest.mark.feature("first-read-gate")
def test_catalogue_enforces_first_read_by_default() -> None:
    assert catalog.defaults()["first-read"] == "enforce"
