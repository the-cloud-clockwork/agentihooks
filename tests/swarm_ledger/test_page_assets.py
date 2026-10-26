import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import page_assets  # noqa: E402

PAGES = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
MOVED = ("SEED_RE", "PAGE_RE", "PAGES", "PALETTE", "TOOLTIPS", "MODULES", "SHELL", "HOME", "TOKEN_RE")
FUNCTIONS = ("static_assets", "page_version", "read_token")


@pytest.mark.parametrize("name", MOVED + FUNCTIONS)
def test_core_serves_the_page_asset_names(name):
    assert getattr(core, name) is getattr(page_assets, name)


def test_asset_paths_sit_beside_the_ledger_code():
    assert (page_assets.PAGES, page_assets.PALETTE, page_assets.TOOLTIPS) == (
        PAGES,
        PAGES / "palette.css",
        PAGES / "tooltips.js",
    )
    assert (page_assets.MODULES, page_assets.SHELL, page_assets.HOME) == (
        PAGES / "static" / "js",
        PAGES / "shell.html",
        PAGES / "home.html",
    )


def test_static_assets_lists_every_page_file_by_its_static_name():
    static = PAGES / "static"
    expected = {
        "palette.css": PAGES / "palette.css",
        "tooltips.js": PAGES / "tooltips.js",
        **{f"css/{p.name}": p for p in sorted((static / "css").glob("*.css"))},
        **{f"js/{p.name}": p for p in sorted((static / "js").glob("*.js"))},
        **{f"home/{p.name}": p for p in sorted((static / "home").glob("*.js"))},
    }
    assert list(page_assets.static_assets().items()) == list(expected.items())


def test_page_version_hashes_both_shells_and_every_named_asset():
    digest = hashlib.sha256((PAGES / "shell.html").read_bytes() + (PAGES / "home.html").read_bytes())
    digest.update(b"a.js\0one")
    digest.update(b"b.css\0two")
    assert page_assets.page_version({"a.js": b"one", "b.css": b"two"}) == digest.hexdigest()[:12]


def test_page_version_reads_the_assets_when_none_are_given():
    assets = {name: path.read_bytes() for name, path in page_assets.static_assets().items()}
    assert page_assets.page_version() == page_assets.page_version(assets)


def test_read_token_finds_the_page_token_or_none():
    token = "abcdefghij_KLMNO-123"
    assert page_assets.read_token(f'<meta name="ledger-token" content="{token}">') == token
    assert page_assets.read_token('<meta name="ledger-token" content="short">') is None
    assert page_assets.read_token("<html></html>") is None


def test_the_seed_and_page_patterns_match_their_tags():
    seed = page_assets.SEED_RE.search('x<script id="ledger-data" type="application/json">\n{"a": 1}\n</script>y')
    assert seed.group(2) == '\n{"a": 1}\n'
    assert page_assets.PAGE_RE.search('<meta name="ledger-page" content="0a1b2c">').group(1) == "0a1b2c"
    assert page_assets.PAGE_RE.search('<meta name="ledger-page" content="xyz">') is None
