"""`agentihooks access env`: export lines for the Cloudflare Access service token in front of the hosted ledger.

The token is read from OpenBao and printed for a shell to evaluate, never written anywhere. Nothing is printed when
OpenBao is unset, unreachable or the record lacks a field, so a launch never waits on it.
"""

import os
import shlex
import sys
from collections.abc import Callable, Mapping
from urllib.request import urlopen

from scripts import openbao_kv

PATH_ENV = "AGENTIHOOKS_ACCESS_TOKEN_PATH"
DEFAULT_PATH = "cloudflare-access-service-token"
NAMES = ("SWARM_ACCESS_CLIENT_ID", "SWARM_ACCESS_CLIENT_SECRET")
TIMEOUT_S = 3
USAGE = "usage: agentihooks access env"


def _opener(request, timeout):
    return urlopen(request, timeout=TIMEOUT_S)


def values(environ: Mapping[str, str], opener: Callable = _opener) -> dict[str, str]:
    bao = openbao_kv.from_env(environ, opener)
    if bao is None:
        return {}
    try:
        record, _ = openbao_kv.read(bao, environ.get(PATH_ENV) or DEFAULT_PATH)
    except openbao_kv.OpenBaoError as exc:
        print(f"agentihooks access: {exc}", file=sys.stderr)
        return {}
    found = {name: record.get(name.replace("SWARM_", "CF_", 1)) for name in NAMES}
    return found if all(isinstance(value, str) and value for value in found.values()) else {}


def main(argv: list[str]) -> int:
    if argv != ["env"]:
        print(USAGE, file=sys.stderr)
        return 2
    found = values(os.environ)
    sys.stdout.write("".join(f"export {name}={shlex.quote(value)}\n" for name, value in sorted(found.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
