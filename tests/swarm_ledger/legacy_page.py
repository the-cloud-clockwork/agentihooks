"""Ledger pages in the pre SQLite file format: the repository imports one on first use, so tests build ledgers this way."""

import html
import re
import secrets
import sys
from pathlib import Path

from scripts.swarm_ledger import HERE

sys.path.insert(0, str(HERE))

from scripts.swarm_ledger import ledger_core  # noqa: E402


def stored_token(page_path):
    """The token of the ledger a legacy page file became once the repository imported it."""
    from scripts.swarm_ledger.repository import repository

    return repository.token(Path(page_path).stem)


def render(doc, slug, port, token=None):
    """The page text for `slug`; the stored ledger of that slug is dropped so the written page is imported fresh."""
    from scripts.swarm_ledger.repository import repository

    for target in (repository, repository.bound(ledger_core)):
        with target.connect() as connection, connection:
            target.purge(slug, connection)
    values = {
        "TITLE": html.escape(doc["title"]),
        "SLUG": slug,
        "PORT": str(int(port)),
        "TOKEN": token or secrets.token_urlsafe(24),
        "DATA": ledger_core.seed_text(doc, 0),
        "PAGE": ledger_core.page_version(),
    }
    page = ledger_core.TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"__LEDGER_(TITLE|SLUG|PORT|TOKEN|DATA|PAGE)__", lambda m: values[m.group(1)], page)


def store(folder, slug, doc):
    """Store `doc` as ledger `slug` in the SQLite record of `folder`, as hooks and launch code read it."""
    from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

    state = {**doc, "_meta": {"rev": 1, **doc.get("_meta", {})}}
    SQLiteLedgerRepository(Path(folder) / DATABASE).import_document(slug, state, replace=True)
