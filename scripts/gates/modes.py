"""Each gate's mode, from AGENTIHOOKS_GATE_<NAME>: enforce denies, observe logs the would-be deny, off skips the gate."""

import re

MODES = ("enforce", "observe", "off")


def env_name(gate_name):
    return "AGENTIHOOKS_GATE_" + re.sub(r"[^A-Z0-9]", "_", gate_name.upper())


def mode(gate, environ):
    chosen = str(environ.get(env_name(gate.name))).strip().lower()
    return chosen if chosen in MODES else gate.default_mode
