"""Ledger pages in the pre SQLite file format: the repository imports one on first use, so tests build ledgers this way."""

import html
import re
import secrets
from pathlib import Path

from scripts.swarm_ledger import ledger_core


def stored_token(page_path):
    """The token of the ledger a legacy page file became once the repository imported it."""
    from scripts.swarm_ledger.repository import repository

    return repository.token(Path(page_path).stem)


def render(doc, slug, port, token=None):
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
