from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
QUICKSTARTS = ("kubernetes-quick-start", "single-machine-quick-start", "add-hive-quick-start")


@pytest.mark.parametrize(
    ("entry", "link"),
    [
        ("README.md", "{}.md)"),
        ("index.md", "{}/)"),
        ("docs/getting-started/installation.md", "{}.md)"),
        ("docs/getting-started/compose-quick-start.md", "{}.md)"),
        ("docs/reference/deployment-modes.md", "{}.md)"),
        ("docs/pillars/hives.md", "{}.md)"),
    ],
)
def test_entry_point_links_every_install_quickstart(entry, link):
    text = (ROOT / entry).read_text()
    assert [slug for slug in QUICKSTARTS if link.format(slug) not in text] == []
