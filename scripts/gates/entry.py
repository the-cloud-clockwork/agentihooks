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
from collections import ChainMap
from collections.abc import Iterator, MutableMapping
from importlib import import_module
from pathlib import Path
from typing import Protocol

from scripts.gates import lift, log, modes
from scripts.gates.base import Call, Decision, Gate, Who

LIFTED = "lifted by the operator: "
FAILED_OPEN = Decision()


class GateRegistry(MutableMapping[str, Gate]):
    def __init__(self, declarations: dict[str, tuple[str, str]]) -> None:
        self._entries: dict[str, Gate | tuple[str, str]] = dict(declarations)

    def __getitem__(self, key: str) -> Gate:
        value = self._entries[key]
        if isinstance(value, tuple):
            module, name = value
            gate: Gate = getattr(import_module(f"scripts.gates.{module}"), name)()
            self._entries[key] = gate
            return gate
        return value

    def __setitem__(self, key: str, value: Gate) -> None:
        self._entries[key] = value

    def __delitem__(self, key: str) -> None:
        del self._entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries


GATES = GateRegistry(
    {
        "identity": ("identity", "PinnedIdentity"),
        "watch": ("watch", "WatchBudget"),
        "subagents": ("subagents", "SubagentBudget"),
        "reruns": ("reruns", "RerunBudget"),
        "intent": ("intent", "IntentGate"),
        "recon": ("recon", "ReconGate"),
        "build": ("build", "BuildGate"),
        "claim-stop": ("claim_stop", "ClaimStop"),
        "expert-stop": ("expert_stop", "ExpertStop"),
        "push-stop": ("push_stop", "PushStop"),
        "quiet": ("quiet", "QuietClaim"),
        "one-push": ("one_push", "OnePush"),
        "brain": ("brain", "BrainGate"),
    }
)
HOST_GATES = GateRegistry(
    {
        "placement": ("placement", "PlacementGate"),
        "prompts": ("prompts", "PromptGuard"),
        "node-caps": ("node_caps", "NodeCaps"),
        "terminations": ("terminations", "Terminations"),
        "sign-ins": ("sign_ins", "SignIns"),
    }
)
BEFORE = {"build": ("recon",)}


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
    named = ChainMap(HOST_GATES, GATES)
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
