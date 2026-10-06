"""The gate entry point a condition shim runs: python -m scripts.gates NAME, the hook payload on stdin.

Exit 2 with the reason on stderr denies the call; exit 0 lets it through; an unknown gate exits 1, which the
condition engine logs and skips.
"""

import json
import sys

from scripts.gates.base import Call, Who
from scripts.gates.identity import PinnedIdentity

GATES = {gate.name: gate for gate in (PinnedIdentity(),)}


def main(argv=None, stdin=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    gate = GATES.get(argv[0]) if argv else None
    if gate is None:
        print(f"agentihooks gate: name one gate of: {', '.join(sorted(GATES))}", file=sys.stderr)
        return 1
    call = Call.from_payload(json.loads((stdin or sys.stdin).read() or "{}"))
    if not gate.matches(call):
        return 0
    decision = gate.decide(call, Who.from_env(environ), None)
    if decision.allowed:
        return 0
    print(decision.reason, file=sys.stderr)
    return 2
