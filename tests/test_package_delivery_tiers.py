from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / "profiles" / "package" / "rules" / "delivery-tiers.md"
PAGE = ROOT / "docs" / "reference" / "delivery-tiers.md"
INDEX = ROOT / "docs" / "reference" / "index.md"


def _body(path: Path) -> str:
    return " ".join(path.read_text().split())


@pytest.mark.parametrize("path", [RULE, PAGE])
@pytest.mark.parametrize(
    "statement",
    [
        "**Pull request**",
        "**Queue**",
        "**Promotion**",
        "A red integration branch keeps merging",
        "promotion does not advance",
        "Promotion owns the slow checks",
        "one fixer task per failing check",
    ],
)
def test_delivery_tiers_state_the_model(path, statement):
    assert statement in _body(path)


def test_delivery_tiers_rule_carries_package_rule_frontmatter():
    head = RULE.read_text().split("---")[1]
    assert "description: " in head
    assert "priority: " in head


def test_reference_index_links_the_delivery_tiers_page():
    assert "| [Delivery tiers](delivery-tiers.md) |" in INDEX.read_text()
