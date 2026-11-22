"""The gate entry point a condition shim runs: python -m scripts.gates NAME [NAME ...], the hook payload on stdin.

Several names are judged in order on the one payload and the first gate that does not let the call through ends it; a
name BEFORE lists is preceded by its gates, so the build gate's callers judge the recon gate first.

Exit 2 with the reason on stderr denies the call; exit 0 lets it through; an unknown gate exits 1, which the
condition engine logs and skips. A gate in observe mode, an operator lift and a gate that raises let the call through
and write the gate log row instead.
"""

import json
import os
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Protocol

from scripts.gates import lift, log, modes
from scripts.gates.base import Call, Decision, Who
from scripts.gates.brain import BrainGate
from scripts.gates.build import BuildGate
from scripts.gates.claim_stop import ClaimStop
from scripts.gates.expert_stop import ExpertStop
from scripts.gates.identity import PinnedIdentity
from scripts.gates.intent import IntentGate
from scripts.gates.node_caps import NodeCaps
from scripts.gates.one_push import OnePush
from scripts.gates.placement import PlacementGate
from scripts.gates.prompts import PromptGuard
from scripts.gates.push_stop import PushStop
from scripts.gates.quiet import QuietClaim
from scripts.gates.recon import ReconGate
from scripts.gates.reruns import RerunBudget
from scripts.gates.sign_ins import SignIns
from scripts.gates.subagents import SubagentBudget
from scripts.gates.terminations import Terminations
from scripts.gates.verdicts import Verdicts
from scripts.gates.watch import WatchBudget

LIFTED = "lifted by the operator: "
FAILED_OPEN = Decision()

GATES = {
    gate.name: gate
    for gate in (
        PinnedIdentity(),
        WatchBudget(),
        SubagentBudget(),
        RerunBudget(),
        IntentGate(),
        ReconGate(),
        BuildGate(),
        ClaimStop(),
        ExpertStop(),
        PushStop(),
        QuietClaim(),
        OnePush(),
        BrainGate(),
    )
}
HOST_GATES = {gate.name: gate for gate in (PlacementGate(), PromptGuard(), NodeCaps(), Terminations(), SignIns())}
BEFORE = {BuildGate.name: (ReconGate.name,)}


class Stdin(Protocol):
    def read(self) -> str: ...


def main(
    argv: list[str] | None = None,
    stdin: Stdin | None = None,
    environ: MutableMapping[str, str] | None = None,
    home: Path | str | None = None,
) -> int:
    """Judge the payload by each named gate, after the gates BEFORE lists for it, in order; the first that does not let
    it through gives the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    named = {**GATES, **HOST_GATES}
    gates = [named.get(first) for name in argv for first in (*BEFORE.get(name, ()), name)]
    if not gates or None in gates:
        print(f"agentihooks gate: name one gate of: {', '.join(sorted(named))}", file=sys.stderr)
        return 1
    payload = json.loads((stdin or sys.stdin).read() or "{}")
    call = Call.from_payload(payload)
    return next((code for gate in gates if (code := evaluate(gate, call, environ, home))), 0)


def _brain_metric(gate, call, who, result):
    if gate.name == "brain":
        from hooks.observability import consultation

        consultation.gate(call, who, result)


def evaluate(gate, call, environ=None, home=None):
    environ = os.environ if environ is None else environ
    if not gate.matches(call):
        return 0
    who = Who.from_env(environ)
    mode = modes.mode(gate, environ, modes.swarm_gates(who.swarm, environ))
    if mode == "off":
        _brain_metric(gate, call, who, "off")
        return 0
    decision = _decide(gate, call, who, home, mode)
    if decision.allowed:
        _brain_metric(gate, call, who, "fail-open" if decision is FAILED_OPEN else "allow")
        return 0
    if lift.lifted(who.swarm, call.session, gate.name, home) or lift.held(
        who.swarm, who.name, who.task, gate.name, home, command=call.command
    ):
        _brain_metric(gate, call, who, "lifted")
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, LIFTED + decision.reason), home)
        return 0
    if mode == "observe":
        _brain_metric(gate, call, who, "observe")
        _record(who, log.Row.of(gate.name, "observe", who, call.tool, decision.reason), home)
        return 0
    _brain_metric(gate, call, who, "deny")
    _record(who, log.Row.of(gate.name, "deny", who, call.tool, decision.reason), home)
    print(decision.reason, file=sys.stderr)
    return 2


def native(name, operation, environ=None):
    from scripts.inbox.seen import receiving_session

    return evaluate(HOST_GATES[name], Call(operation, session=receiving_session(environ)), environ)


def _decide(gate, call, who, home, mode="enforce"):
    try:
        state = Verdicts(who.swarm, gate.name, home)
        if isinstance(gate, IntentGate):
            return gate.decide(call, who, state, mode)
        return gate.decide(call, who, state)
    except Exception as exc:  # a crashed gate lets the call through, counted in the gate log
        detail = "gate evaluation failed" if gate.name == "brain" else f"{type(exc).__name__}: {exc}"
        _record(who, log.Row.of(gate.name, "fail-open", who, call.tool, detail), home)
        return FAILED_OPEN


def _record(who, row, home):
    if who.swarm:
        log.append(who.swarm, row, home)
