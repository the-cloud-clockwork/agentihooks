import pytest

from scripts.headless import tiers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("harness", "tier", "expected"),
    [
        ("claude", "high", "opus"),
        ("claude", "low", "sonnet"),
        ("codex", "high", "gpt-6.1-sol"),
        ("codex", "low", "gpt-6-luna"),
    ],
)
def test_each_tier_has_a_default_model_per_harness(harness, tier, expected):
    assert tiers.model(harness, tier, {}) == expected


@pytest.mark.parametrize(
    ("harness", "tier", "name"),
    [
        ("claude", "high", "AGENTIHOOKS_TIER_HIGH_CLAUDE"),
        ("claude", "low", "AGENTIHOOKS_TIER_LOW_CLAUDE"),
        ("codex", "high", "AGENTIHOOKS_TIER_HIGH_CODEX"),
        ("codex", "low", "AGENTIHOOKS_TIER_LOW_CODEX"),
    ],
)
def test_the_environment_overrides_a_tier(harness, tier, name):
    assert tiers.model(harness, tier, {name: "custom"}) == "custom"


def test_an_empty_override_keeps_the_default():
    assert tiers.model("claude", "low", {"AGENTIHOOKS_TIER_LOW_CLAUDE": ""}) == "sonnet"


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError) as caught:
        tiers.model("claude", "middle", {})
    assert str(caught.value) == "no middle tier for claude"
