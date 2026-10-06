"""Every swarm gate by name with its default mode: the keys swarm set takes and the controls the ledger page shows."""

from scripts.gates import modes, talk
from scripts.gates.claims import GATE as CLAIMS
from scripts.gates.entry import GATES


def defaults():
    from scripts.swarm.trace_plan import GATE as TRACE_PLAN

    gates = {**GATES, CLAIMS.name: CLAIMS, TRACE_PLAN.name: TRACE_PLAN}
    return {name: gate.default_mode for name, gate in gates.items()} | {talk.NAME: talk.DEFAULT_MODE}


def current(gates):
    return {
        name: gates.get(name) if gates.get(name) in modes.MODES else default for name, default in defaults().items()
    }
