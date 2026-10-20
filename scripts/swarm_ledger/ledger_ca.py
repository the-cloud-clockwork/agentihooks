"""The hosted ledger's CA certificate, copied from OpenBao into the file ledger clients trust."""

import os
import ssl
from collections.abc import Callable
from pathlib import Path

from scripts import openbao_kv
from scripts.swarm_ledger import ledger_link

OPENBAO_PATH = "k8s/agentihooks-swarm-ledger-ca"
FIELD = "ca.crt"


def certificate(pem: object) -> bool:
    if not isinstance(pem, str) or not pem.strip() or "PRIVATE KEY" in pem:
        return False
    try:
        ssl.create_default_context(cadata=pem)
    except ssl.SSLError:
        return False
    return True


def install(environ=os.environ, opener: Callable | None = None) -> str:
    bao = openbao_kv.from_env(environ, opener)
    if bao is None:
        return "ledger CA: no OpenBao"
    try:
        found, _ = openbao_kv.read(bao, OPENBAO_PATH)
    except openbao_kv.OpenBaoError:
        return "ledger CA: OpenBao unavailable"
    pem = found.get(FIELD)
    if not certificate(pem):
        return f"ledger CA: no certificate in OpenBao {OPENBAO_PATH}"
    target = ledger_link.ca_file(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pem)
    return f"ledger CA: {target}"


def served(environ=os.environ) -> str | None:
    pem = environ.get(ledger_link.CA_ENV, "")
    if not pem.strip():
        found = ledger_link.ca_file(environ)
        if not found.is_file():
            return None
        pem = found.read_text()
    return pem if certificate(pem) else None


def place(home: Path, pem: str | None, environ=os.environ) -> Path | None:
    if not certificate(pem):
        return None
    target = ledger_link.ca_file({**environ, "AGENTIHOOKS_HOME": str(home)})
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}")
    staged.write_text(pem)
    os.replace(staged, target)
    return target
