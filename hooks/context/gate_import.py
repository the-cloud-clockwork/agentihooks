"""Reports a gate condition whose child could not import the gates package, from the healthy hook process that ran it.

The gate comes from the condition's own command, its main(["NAME"]) call or its -m scripts.gates NAME launch, and is
reported only when the gate registry holds that name and the child's last stderr line is an import failure.
"""

import re
import sys
from pathlib import Path
from typing import Any

NAMED = re.compile(r"""\bmain\(\s*\[\s*["']([\w-]+)["']\s*\]\s*\)""")
LAUNCHED = re.compile(r"-m\s+scripts\.gates\s+([\w-]+)")
RAISED = re.compile(r"(?:ModuleNotFoundError|ImportError): .+")
IMPORTING = re.compile(
    r"^\s+(?:from\s+scripts\.gates[\w.]*\s+import\s|import\s+scripts\.gates\b)|, in _get_module_details$", re.MULTILINE
)
LAUNCHER = re.compile(r"(\S+): ((?:No module named|Error while finding module specification for) '?scripts\b.*)")


def gate_name(path: Path) -> str:
    """The one gate a condition file runs, or an empty name when it runs none or several."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    found = {*NAMED.findall(text), *LAUNCHED.findall(text)}
    return found.pop() if len(found) == 1 else ""


def import_failure(stderr: str, ext: str) -> tuple[str, str] | None:
    """The import error of a child that died importing the gates package and the interpreter that hit it, else None."""
    last = stderr.rstrip().rpartition("\n")[2].strip()
    if RAISED.fullmatch(last) and IMPORTING.search(stderr):
        return last, sys.executable if ext == ".py" else "an unknown interpreter"
    if launched := LAUNCHER.fullmatch(last):
        return launched[2], launched[1]
    return None


def report(entry: dict[str, Any], run: dict[str, Any], payload: dict[str, Any]) -> None:
    """Report a failed run of a registered gate's condition that died importing the gates package."""
    failed = import_failure(run.get("stderr") or "", entry["ext"])
    gate = gate_name(Path(entry["path"])) if failed else ""
    if not failed or not gate:
        return
    try:
        from scripts.gates import entry as gates
        from scripts.gates import import_failure as reporter
    except ImportError as exc:
        print(f"agentihooks gate {gate}: import failure of {entry['path']} not reported: {exc}", file=sys.stderr)
        return
    if gate in gates.GATES or gate in gates.HOST_GATES:
        error, interpreter = failed
        reporter.report(reporter.Failure(gate, error, interpreter, str(payload.get("tool_name") or "")))
