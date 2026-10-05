"""The ledger page link handed to the operator, from the ledger server's configured host and port."""

import os
import urllib.request

START = "agentihooks ledger serve --ensure"


def base():
    return f"http://{os.environ.get('LEDGER_HOST', '127.0.0.1')}:{os.environ.get('LEDGER_PORT', '8765')}"


def page_url(slug):
    return f"{base()}/{slug}"


def answering():
    try:
        with urllib.request.urlopen(f"{base()}/healthz", timeout=1):
            return True
    except (OSError, ValueError):
        return False


def page_line(slug):
    line = f"Ledger page: {page_url(slug)} (open it to follow and steer the work)"
    if answering():
        return line
    return f"{line}. The ledger server is not answering: start it with {START}"
