"""The hosted ledger's CA certificate, copied from OpenBao into the file ledger clients trust."""

import os
import ssl
from collections.abc import Callable

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
