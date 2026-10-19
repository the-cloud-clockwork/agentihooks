"""Ledger pages in the pre SQLite file format: the repository imports one on first use, so tests build ledgers this way."""

import html
import secrets
import sys
from pathlib import Path

from scripts.swarm_ledger import HERE

sys.path.insert(0, str(HERE))

from scripts.swarm_ledger import ledger_core  # noqa: E402

PAGE = (
    "<!doctype html>\n<title>{title}</title>\n"
    '<meta name="ledger-token" content="{token}">\n'
    '<script id="ledger-data" type="application/json">{data}</script>\n'
)


def stored_token(page_path):
    """The token of the ledger a legacy page file became once the repository imported it."""
    from scripts.swarm_ledger.storage import default as default_repository

    repository = default_repository()

    return repository.token(Path(page_path).stem)


def paths(slug, core=ledger_core):
    """The page file and state file a pre SQLite ledger kept in the ledger folder of `core`."""
    if not isinstance(slug, str) or not core.SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    return core.LEDGER_DIR / f"{slug}.html", core.LEDGER_DIR / f"{slug}.json"


def render(doc, slug, port, token=None):
    """The page text for `slug`; the stored ledger of that slug is dropped so the written page is imported fresh."""
    from scripts.swarm_ledger.storage import default as default_repository

    repository = default_repository()

    for target in (repository, repository.bound(ledger_core)):
        with target.connect() as connection, connection:
            target.purge(slug, connection)
    return PAGE.format(
        title=html.escape(doc["title"]),
        token=token or secrets.token_urlsafe(24),
        data=ledger_core.seed_text(doc, 0),
    )


def store(folder, slug, doc):
    """Store `doc` as ledger `slug` in the SQLite record of `folder`, as hooks and launch code read it."""
    from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

    state = {**doc, "_meta": {"rev": 1, **doc.get("_meta", {})}}
    SQLiteLedgerRepository(Path(folder) / DATABASE).import_document(slug, state, replace=True)
