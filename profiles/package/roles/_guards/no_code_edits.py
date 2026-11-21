import json
import os
import re
import shlex
import sys
from itertools import takewhile
from pathlib import Path

PLANNERS = {"master", "planner"}
BUILTIN_WRITES = {"edit", "write", "notebookedit"}
SERENA_WRITES = {
    f"mcp__serena__{name}"
    for name in (
        "replace_symbol_body",
        "insert_after_symbol",
        "insert_before_symbol",
        "rename_symbol",
        "replace_content",
        "replace_in_files",
        "safe_delete_symbol",
    )
}
SHELLS = {"bash", "sh", "zsh", "dash"}
WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g"}),
    "env": frozenset({"-u"}),
    "nice": frozenset({"-n"}),
    **dict.fromkeys(("command", "nohup", "exec", "time", "timeout"), frozenset()),
}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}
COPIES = {"cp", "ln", "install", "rsync"}
WRITERS = COPIES | {"mv", "rm", "rmdir", "touch", "mkdir", "chmod", "chown", "truncate", "tee", "unlink"}
IN_PLACE = {"sed", "perl"}
GIT_READS = {
    "status",
    "log",
    "diff",
    "show",
    "fetch",
    "rev-parse",
    "rev-list",
    "merge-base",
    "blame",
    "grep",
    "ls-files",
    "ls-tree",
    "ls-remote",
    "cat-file",
    "describe",
    "shortlog",
    "for-each-ref",
    "show-ref",
    "name-rev",
    "range-diff",
    "diff-tree",
    "cherry",
    "version",
    "help",
}
GH_WRITES = {("pr", "checkout"), ("repo", "clone")}
EXECS = {"-exec", "-execdir", "-ok", "-okdir"}
XARGS_VALUES = {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"}
DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
IN_PLACE_FLAG = re.compile(r"-[a-zA-Z]*i|--in-place")
SHELL_SCRIPT_FLAG = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")
FD = re.compile(r"\d*-?")
SHELL_TOKEN = re.compile(
    r"(?P<out>\d*(?:>>|>\||>&|>)|&>>?)"
    r"|(?P<into>\d*(?:<<<|<<-?|<&|<))"
    r"|(?P<op>&&|\|\||[;&|()`\n])"
    r"""|(?P<word>(?:[^\s;&|<>()`'"\\]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+)"""
)


def _allowed_roots(role: str, home: Path) -> list[Path]:
    from hooks.context import master_coding

    roots = [(home / "scratchpad").resolve()]
    if role in PLANNERS:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        roots.append((config / "plans").resolve())
    swarm = os.environ.get("AGENTIHOOKS_SWARM", "")
    task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
    if not master_coding.MASTER_TASK.fullmatch(task) and all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) for value in (swarm, task)
    ):
        folder = home.resolve() / ".agentihooks" / "swarm" / swarm / "tasks" / task
        if folder.resolve() == folder:
            roots.append(folder)
    return roots


def _inside(tool: str, value: str, cwd: Path, roots: list[Path]) -> bool:
    path = Path(value).expanduser()
    if tool == "mcp__serena__rename_symbol" or (tool in SERENA_WRITES and not path.is_absolute()):
        return False
    target = (path if path.is_absolute() else cwd / path).resolve()
    return any(target.is_relative_to(root) for root in roots)


def hotfix_open(payload: dict, role: str) -> bool:
    """The operator typed allow-master-coding in this master session and has not closed it."""
    if role != "master":
        return False
    from hooks.context import master_coding

    return master_coding.is_open(str(payload.get("session_id") or ""))


def _delegated(payload: dict, role: str) -> bool:
    from hooks.context import master_coding

    return (
        role == "master"
        and bool(payload.get("agent_id"))
        and master_coding.is_master(str(payload.get("session_id") or ""))
    )


def _unquoted(word: str) -> str:
    return "".join(shlex.split(word))


def _shell_commands(command: str) -> list[tuple[list[str], list[str]]]:
    """Each simple command in *command* as its words and the files its output redirects write."""
    from hooks.context.credential_guard import strip_heredocs

    commands: list[tuple[list[str], list[str]]] = [([], [])]
    pending = ""
    for match in SHELL_TOKEN.finditer(strip_heredocs(command.replace("\\\n", " "))):
        kind, text = match.lastgroup or "", match.group()
        if kind == "op":
            commands.append(([], []))
        elif kind != "word":
            pending = "dup" if kind == "out" and text.endswith("&") else kind
            continue
        elif pending == "out" or (pending == "dup" and not FD.fullmatch(text)):
            commands[-1][1].append(_unquoted(text))
        elif not pending:
            commands[-1][0].append(_unquoted(text))
        pending = ""
    return commands


def _unwrapped(words: list[str]) -> list[str]:
    rest = list(words)
    while rest and (rest[0] in KEYWORDS or ASSIGNMENT.match(rest[0])):
        rest.pop(0)
    wrapper = Path(rest[0]).name if rest else ""
    if wrapper not in WRAPPERS:
        return rest
    rest.pop(0)
    while rest and rest[0].startswith("-"):
        if rest.pop(0) in WRAPPERS[wrapper] and rest:
            rest.pop(0)
    if wrapper == "timeout" and rest:
        rest.pop(0)
    return _unwrapped(rest)


def _resolved(value: str, cwd: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def _within(value: str, cwd: Path, roots: list[Path]) -> bool:
    return value in DEVICES or any(_resolved(value, cwd).is_relative_to(root) for root in roots)


def _write_operands(words: list[str]) -> list[str]:
    head = Path(words[0]).name if words else ""
    operands = [word for word in words[1:] if not word.startswith("-")]
    if head in COPIES:
        return operands[-1:]
    in_place = head in IN_PLACE and any(IN_PLACE_FLAG.match(word) for word in words[1:])
    return operands if head in WRITERS or in_place else []


def _git_reason(words: list[str]) -> str:
    head = Path(words[0]).name if words else ""
    if head == "gh" and tuple(words[1:3]) in GH_WRITES:
        return "runs gh " + " ".join(words[1:3])
    rest = iter(words[1:] if head == "git" else [])
    for word in rest:
        if word in ("-C", "-c"):
            next(rest, "")
        elif not word.startswith("-"):
            return "" if word in GIT_READS else f"runs git {word}"
    return ""


def _xargs_command(words: list[str]) -> list[str]:
    rest = list(words)
    while rest and rest[0].startswith("-"):
        if rest.pop(0) in XARGS_VALUES and rest:
            rest.pop(0)
    return rest


def _nested(words: list[str]) -> list[str]:
    """The command lines *words* hands on: a shell's -c script, eval's words, find -exec and xargs commands."""
    head = Path(words[0]).name if words else ""
    if head in SHELLS:
        return [script for flag, script in zip(words[1:], words[2:]) if SHELL_SCRIPT_FLAG.fullmatch(flag)]
    if head == "eval":
        return [" ".join(words[1:])]
    if head == "find":
        ends = (";", "+")
        return [
            shlex.join(takewhile(lambda w: w not in ends, words[i + 1 :])) for i, w in enumerate(words) if w in EXECS
        ]
    if head == "xargs":
        return [shlex.join([*_xargs_command(words[1:]), "{}"])]
    return []


def _nested_reason(words: list[str], cwd: Path, roots: list[Path]) -> str:
    head = Path(words[0]).name if words else ""
    if head == "find" and "-delete" in words:
        return "deletes files with find -delete"
    if head in SHELLS and all(word.startswith("-") for word in words[1:]):
        return f"feeds a script to {head} on its input"
    for script in _nested(words):
        found = _shell_reason(script, cwd, roots)
        if found:
            return found
    return ""


def _shell_reason(command: str, cwd: Path, roots: list[Path]) -> str:
    """What *command* writes outside *roots* or which git write it runs, or empty when it does neither."""
    for words, targets in _shell_commands(command):
        words = _unwrapped(words)
        if words[:1] == ["cd"] and len(words) > 1:
            cwd = _resolved(words[1], cwd)
        outside = [value for value in targets + _write_operands(words) if not _within(value, cwd, roots)]
        found = (f"writes {outside[-1]}" if outside else "") or _git_reason(words) or _nested_reason(words, cwd, roots)
        if found:
            return found
    return ""


def _shell_deny(payload: dict, role: str, home: Path) -> str:
    cwd = Path(payload.get("cwd") or os.getcwd())
    found = _shell_reason(str(payload["tool_input"].get("command") or ""), cwd, _allowed_roots(role, home))
    if not found:
        return ""
    return (
        f"{role} cannot write through the shell: the command {found}. "
        "Reads, ~/scratchpad and the task folder stay open; send the change to its author."
    )


def deny_reason(payload: dict, role: str, home: Path) -> str:
    tool = payload["tool_name"].lower()
    if tool == "bash":
        return _shell_deny(payload, role, home)
    if tool not in BUILTIN_WRITES | SERENA_WRITES:
        return ""
    inputs = payload["tool_input"]
    paths = [inputs[key] for key in ("file_path", "notebook_path", "relative_path") if key in inputs]
    paths.extend(inputs.get("file_paths", []))
    cwd = Path(payload.get("cwd") or os.getcwd())
    roots = _allowed_roots(role, home)
    if _delegated(payload, role) or hotfix_open(payload, role):
        from hooks.context.master_coding import closed_in_window

        if any(closed_in_window(value, cwd) for value in paths):
            return "master hotfix window: condition folders and credential files stay closed."
        roots.append((home / "dev" / "worktrees").resolve())
        relative = all(value and not Path(value).expanduser().is_absolute() for value in paths)
        if tool in SERENA_WRITES - {"mcp__serena__rename_symbol"} and paths and relative:
            return ""
    if paths and all(_inside(tool, value, cwd, roots) for value in paths):
        return ""
    return f"{role} cannot edit outside ~/scratchpad; send the change to its author."


def main(role: str) -> int:
    try:
        reason = deny_reason(json.load(sys.stdin), role, Path.home())
    except Exception:
        reason = f"{role} cannot evaluate the coding window; edits stay closed."
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
