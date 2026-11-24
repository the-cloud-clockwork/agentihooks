"""Swarm set refuses a whole number written with non ASCII digits the way it refuses a letter."""

from typing import Any

import pytest

from scripts.swarm.store import RedisStore
from tests.swarm import test_cli

env = test_cli.env
pytestmark = pytest.mark.unit

SLUG = "sw"
SUPERSCRIPT_TWO = "²"
FULLWIDTH_THREE = "３"


@pytest.mark.feature("swarm-set-ascii-whole-numbers")
@pytest.mark.parametrize(
    ("key", "digits"),
    [
        ("max-eng-agents", SUPERSCRIPT_TWO),
        ("max-eng-agents", FULLWIDTH_THREE),
        ("memory-per-agent", SUPERSCRIPT_TWO),
        ("swap-floor", SUPERSCRIPT_TWO),
        ("placement-nodes", SUPERSCRIPT_TWO),
    ],
)
def test_set_refuses_a_non_ascii_digit_like_any_other_non_number(
    env: tuple[RedisStore, Any, Any], capsys: pytest.CaptureFixture[str], key: str, digits: str
) -> None:
    store, _, _ = env
    test_cli.run(SLUG, "create", "--repo", "/repo")
    before = store.config(SLUG)
    capsys.readouterr()
    assert test_cli.run(SLUG, "set", f"{key}=x") == 1
    refusal = capsys.readouterr().err
    assert refusal.startswith("swarm: ")
    assert test_cli.run(SLUG, "set", f"{key}={digits}") == 1
    assert capsys.readouterr().err == refusal
    assert store.config(SLUG) == before
