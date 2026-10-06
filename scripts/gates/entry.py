"""The gate entry point a condition shim runs: python -m scripts.gates NAME, the hook payload on stdin.

Exit 2 with the reason on stderr denies the call; exit 0 lets it through; an unknown gate exits 1, which the
condition engine logs and skips. A gate in observe mode, an operator lift and a gate that raises let the call through
and write the gate log row instead.
"""

import json
import os
import sys

from scripts.gates import lift, log, modes
from scripts.gates.base import Call, Decision, Who
from scripts.gates.identity import PinnedIdentity
from scripts.gates.reruns import RerunBudget
from scripts.gates.subagents import SubagentBudget
from scripts.gates.verdicts import Verdicts

LIFTED = "lifted by the operator: "

GATES = {gate.name: gate for gate in (PinnedIdentity(), SubagentBudget(), RerunBudget())}


def main(argv=None, stdin=None, environ=None, home=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    gate = GATES.get(argv[0]) if argv else None
    if gate is None:
        print(f"agentihooks gate: name one gate of: {', '.join(sorted(GATES))}", file=sys.stderr)
        return 1
    payload = json.loads((stdin or sys.stdin).read() or "{}")
    call = Call.from_payload(payload)
    if modes.mode(gate, environ) == "off" or not gate.matches(call):
        return 0
    who = Who.from_env(environ)
    decision = _decide(gate, call, who, home)
    if decision.allowed:
        return 0
    if lift.lifted(who.swarm, payload.get("session_id"), gate.name, home):
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, LIFTED + decision.reason), home)
        return 0
    if modes.mode(gate, environ) == "observe":
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, decision.reason), home)
        return 0
    _record(who, log.Row.of(gate.name, "deny", who, call.tool, decision.reason), home)
    print(decision.reason, file=sys.stderr)
    return 2


def _decide(gate, call, who, home):
    try:
        return gate.decide(call, who, Verdicts(who.swarm, gate.name, home))
    except Exception as exc:  # a crashed gate lets the call through, counted in the gate log
        _record(who, log.Row.of(gate.name, "fail-open", who, call.tool, f"{type(exc).__name__}: {exc}"), home)
        return Decision()


def _record(who, row, home):
    if who.swarm:
        log.append(who.swarm, row, home)
