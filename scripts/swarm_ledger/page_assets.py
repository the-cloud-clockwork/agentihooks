import hashlib
import re
from pathlib import Path

SEED_RE = re.compile(r'(<script id="ledger-data" type="application/json">)(.*?)(</script>)', re.S)
PAGE_RE = re.compile(r'<meta name="ledger-page" content="([0-9a-f]+)">')
PAGES = Path(__file__).resolve().parent
PALETTE = PAGES / "palette.css"
TOOLTIPS = PAGES / "tooltips.js"
MODULES = PAGES / "static" / "js"
SHELL = PAGES / "shell.html"
HOME = PAGES / "home.html"
TOKEN_RE = re.compile(r'<meta name="ledger-token" content="([A-Za-z0-9_-]{16,})">')


def static_assets():
    """Every file a page loads, by its path under /static/<page version>/."""
    static = MODULES.parent
    return {
        "palette.css": PALETTE,
        "tooltips.js": TOOLTIPS,
        **{f"css/{p.name}": p for p in sorted((static / "css").glob("*.css"))},
        **{f"js/{p.name}": p for p in sorted(MODULES.glob("*.js"))},
        **{f"home/{p.name}": p for p in sorted((static / "home").glob("*.js"))},
    }


def page_version(assets=None):
    if assets is None:
        assets = {name: path.read_bytes() for name, path in static_assets().items()}
    digest = hashlib.sha256(SHELL.read_bytes() + HOME.read_bytes())
    for name, data in assets.items():
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()[:12]


def read_token(html):
    match = TOKEN_RE.search(html)
    return match.group(1) if match else None
