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
from scripts.gates.build import BuildGate
from scripts.gates.claim_stop import ClaimStop
from scripts.gates.identity import PinnedIdentity
from scripts.gates.intent import IntentGate
from scripts.gates.one_push import OnePush
from scripts.gates.placement import PlacementGate
from scripts.gates.prompts import PromptGuard
from scripts.gates.push_stop import PushStop
from scripts.gates.quiet import QuietClaim
from scripts.gates.reruns import RerunBudget
from scripts.gates.subagents import SubagentBudget
from scripts.gates.verdicts import Verdicts
from scripts.gates.watch import WatchBudget

LIFTED = "lifted by the operator: "

GATES = {
    gate.name: gate
    for gate in (
        PinnedIdentity(),
        WatchBudget(),
        SubagentBudget(),
        RerunBudget(),
        IntentGate(),
        BuildGate(),
        ClaimStop(),
        PushStop(),
        QuietClaim(),
        OnePush(),
    )
}
HOST_GATES = {gate.name: gate for gate in (PlacementGate(), PromptGuard())}


def main(argv=None, stdin=None, environ=None, home=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    named = {**GATES, **HOST_GATES}
    gate = named.get(argv[0]) if argv else None
    if gate is None:
        print(f"agentihooks gate: name one gate of: {', '.join(sorted(named))}", file=sys.stderr)
        return 1
    payload = json.loads((stdin or sys.stdin).read() or "{}")
    call = Call.from_payload(payload)
    if not gate.matches(call):
        return 0
    who = Who.from_env(environ)
    mode = modes.mode(gate, environ, modes.swarm_gates(who.swarm, environ))
    if mode == "off":
        return 0
    decision = _decide(gate, call, who, home, mode)
    if decision.allowed:
        return 0
    if lift.lifted(who.swarm, payload.get("session_id"), gate.name, home) or lift.agent_lifted(
        who.swarm, who.name, gate.name, home
    ):
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, LIFTED + decision.reason), home)
        return 0
    if mode == "observe":
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, decision.reason), home)
        return 0
    _record(who, log.Row.of(gate.name, "deny", who, call.tool, decision.reason), home)
    print(decision.reason, file=sys.stderr)
    return 2


def _decide(gate, call, who, home, mode="enforce"):
    try:
        state = Verdicts(who.swarm, gate.name, home)
        if isinstance(gate, IntentGate):
            return gate.decide(call, who, state, mode)
        return gate.decide(call, who, state)
    except Exception as exc:  # a crashed gate lets the call through, counted in the gate log
        _record(who, log.Row.of(gate.name, "fail-open", who, call.tool, f"{type(exc).__name__}: {exc}"), home)
        return Decision()


def _record(who, row, home):
    if who.swarm:
        log.append(who.swarm, row, home)
