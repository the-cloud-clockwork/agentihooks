"""Reports a gate condition whose child could not import the gates package, from the healthy hook process that ran it.

The gates come from the condition's own command, its main([...]) call or its -m scripts.gates launch, and each one the
gate registry holds is reported when the child's last stderr line is an import failure of the gates package. The
interpreter is the one the child ran under: the hook's own for a Python condition, else the one its launch error line
names, the literal path its shell command launches, or the agentihooks command's shebang interpreter it reads.
"""

import re
import shutil
import sys
from pathlib import Path
from typing import Any

NAMED = re.compile(r"\bmain\(\s*\[([^\]]*)\]\s*\)")
QUOTED = re.compile(r"""["']([\w-]+)["']""")
LAUNCHED = re.compile(r"-m\s+scripts\.gates((?:[ \t]+[\w-]+)+)")
LITERAL = re.compile(r"""["']?(/[^\s"']+)["']?(?:\s+-\w+)*\s+-m\s+scripts\.gates\b""")
SHEBANG = "command -v agentihooks"
RAISED = re.compile(r"(?:ModuleNotFoundError|ImportError): .+")
IMPORTING = re.compile(
    r"^\s+(?:from\s+scripts\.gates[\w.]*\s+import\s|import\s+scripts\.gates\b)|, in _get_module_details$", re.MULTILINE
)
LAUNCHER = re.compile(r"(\S+): ((?:No module named|Error while finding module specification for) '?scripts\b.*)")
UNKNOWN = "an unknown interpreter"


def read_condition(path: Path) -> str:
    """The condition file's text, or an empty text when it cannot be read."""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def gate_names(text: str) -> list[str]:
    """The gates a condition runs, from its main([...]) call and its -m scripts.gates launch, once each in order."""
    named = [name for listed in NAMED.findall(text) for name in QUOTED.findall(listed)]
    launched = [name for given in LAUNCHED.findall(text) for name in given.split()]
    return list(dict.fromkeys(named + launched))


def launcher(text: str) -> str:
    """The interpreter a shell condition launches the gates package with, or an empty name when it cannot be told."""
    if literal := LITERAL.search(text):
        return literal[1]
    command = shutil.which("agentihooks") if SHEBANG in text else None
    if command is None:
        return ""
    try:
        first = Path(command).read_text(errors="replace").partition("\n")[0]
    except OSError:
        return ""
    return first.removeprefix("#!").strip()


def import_failure(stderr: str) -> tuple[str, str] | None:
    """The import error of a child that died importing the gates package and the interpreter its launch error line
    names, empty for a traceback; None when the child died of anything else."""
    last = stderr.rstrip().rpartition("\n")[2].strip()
    if RAISED.fullmatch(last) and IMPORTING.search(stderr):
        return last, ""
    if launched := LAUNCHER.fullmatch(last):
        return launched[2], launched[1]
    return None


def report(entry: dict[str, Any], run: dict[str, Any], payload: dict[str, Any]) -> None:
    """Report each registered gate of a failed condition whose child died importing the gates package."""
    failed = import_failure(run.get("stderr") or "")
    text = read_condition(Path(entry["path"])) if failed else ""
    gates = gate_names(text)
    if not failed or not gates:
        return
    try:
        from scripts.gates import entry as registry
        from scripts.gates import import_failure as reporter
    except ImportError as exc:
        names = ", ".join(gates)
        print(f"agentihooks gate {names}: import failure of {entry['path']} not reported: {exc}", file=sys.stderr)
        return
    error, named = failed
    interpreter = named or _ran_under(entry["ext"], text)
    tool = str(payload.get("tool_name") or "")
    for gate in gates:
        if gate in registry.GATES or gate in registry.HOST_GATES:
            reporter.report(reporter.Failure(gate, error, interpreter, tool))


def _ran_under(ext: str, text: str) -> str:
    return sys.executable if ext == ".py" else launcher(text) or UNKNOWN
