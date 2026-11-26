import ast
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, cast

from scripts.gates.base import Call, Decision, Who

REASON = "Run agentihooks seat brief before using other tools in this seated session."
SESSION = re.compile(r"[A-Za-z0-9_-]+")
CONDITION = re.compile(
    r"(?:mcp__agentihooks(?:__|\.)|mcp_agentihooks_|)(?:condition_list|condition_show|condition_set|condition_clear)"
)
CANARY_EXIT = ast.dump(ast.parse("sys.exit(main(sys.argv[2]))").body[0])


def marker_path(session: str) -> Path:
    if not SESSION.fullmatch(session):
        raise ValueError("invalid first read session id")
    home = os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks"
    return Path(home) / "first-read" / session


def membership_path(session: str) -> Path:
    return marker_path(session).parent / ".members" / session


def _member(who: Who | None = None) -> bool:
    if os.environ.get("AGENTIHOOKS_QUICK_SEAT"):
        return True
    identity: Who = who or cast(Any, Who).from_env()
    return identity.pinned


def _command(command: str) -> str:
    from hooks.targets.normalizer import exec_calls

    calls = exec_calls(command)
    if not calls:
        return command
    if len(calls) != 1 or len(re.findall(r"\btools\b", command)) != 1:
        return ""
    return str(calls[0].get("cmd") or "")


def _words(command: str) -> list[str]:
    try:
        words = shlex.split(_command(command))
        return [] if words and any(char in words[0] for char in "$`<>(){}|;&\n") else words
    except ValueError:
        return []


def is_brief(command: str) -> bool:
    words = _words(command)
    return len(words) == 3 and Path(words[0]).name == "agentihooks" and words[1:] == ["seat", "brief"]


def _canary_statement(statement: ast.stmt) -> bool:
    if ast.dump(statement) == CANARY_EXIT:
        return True
    if isinstance(statement, ast.Import):
        return [(alias.name, alias.asname) for alias in statement.names] == [("sys", None)]
    if isinstance(statement, ast.ImportFrom):
        return statement.module == "scripts.profiles.binding" and [
            (alias.name, alias.asname) for alias in statement.names
        ] == [("main", None)]
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        ast.unparse(call.func) == "sys.path.insert"
        and not call.keywords
        and len(call.args) == 2
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 0
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    )


def _canary(command: str) -> bool:
    words = _words(command)
    if len(words) != 5 or not re.fullmatch(r"python[0-9.]*", Path(words[0]).name):
        return False
    if words[1] != "-c" or words[3] != "--canary" or not re.fullmatch(r"[0-9a-fA-F]+", words[4]):
        return False
    try:
        statements = ast.parse(words[2]).body
    except SyntaxError:
        return False
    return any(ast.dump(statement) == CANARY_EXIT for statement in statements) and all(
        _canary_statement(statement) for statement in statements
    )


def _opening(call: Call, who: Who) -> bool:
    from scripts.herdr_panes import run_folder

    args: dict[str, Any] = cast("Any", call).tool_input
    path = Path(str(args.get("file_path") or args.get("path") or ""))
    if path.parent.resolve() != run_folder(dict(os.environ)).resolve():
        return False
    names = {who.name, who.name.replace("@", "-"), os.environ.get("AGENTIHOOKS_QUICK_SEAT", "")}
    prefixes = {"".join(char if char.isalnum() or char in "._-" else "_" for char in name) for name in names if name}
    return any(
        re.fullmatch(re.escape(prefix) + r"-\d{6}-\d{6}-\d+\.(?:prompt|opening)", path.name) for prefix in prefixes
    )


def _text_succeeded(response: str) -> bool:
    try:
        decoded: object = json.loads(response)
    except ValueError:
        if response.startswith("Script completed") and "\nOutput:\n" in response:
            return _succeeded(response.split("\nOutput:\n", 1)[1].strip())
        header = response.split("\nFinal output:", 1)[0]
        status = re.search(r"^Process exited with code (-?\d+)$", header, re.MULTILINE)
        return status is not None and status.group(1) == "0"
    return _succeeded(decoded)


def _succeeded(response: object) -> bool:
    if isinstance(response, str):
        return _text_succeeded(response)
    if not isinstance(response, dict):
        return False
    result = cast("dict[str, Any]", response)
    if result.get("isError") or result.get("is_error") or result.get("interrupted") or result.get("session_id"):
        return False
    if "exit_code" in result:
        return type(result["exit_code"]) is int and result["exit_code"] == 0
    if result.get("type") == "text":
        return _succeeded(result.get("text"))
    content = result.get("content")
    if isinstance(content, list):
        blocks = cast("list[object]", content)
        return bool(blocks) and all(_succeeded(block) for block in blocks)
    return "stdout" in result and result.get("interrupted") is False


def mark(payload: dict[str, Any]) -> None:
    if payload.get("tool_name") != "Bash":
        return
    call = Call.from_payload(payload)
    if not call.session or not is_brief(call.command) or not _succeeded(payload.get("tool_response")) or not _member():
        return
    path = marker_path(call.session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1")


def reset(payload: dict[str, Any]) -> None:
    session = str(payload.get("session_id") or "")
    if not session:
        return
    if payload.get("source") == "compact":
        marker_path(session).unlink(missing_ok=True)
    path = membership_path(session)
    if _member():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1")
    else:
        path.unlink(missing_ok=True)


class FirstRead:
    name = "first-read"
    default_mode = "observe"

    def matches(self, call: Call) -> bool:
        if not _member() or not call.session:
            return False
        try:
            return not marker_path(call.session).is_file()
        except (OSError, ValueError):
            return True

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        if not _member(who) or not call.session:
            return Decision()
        if call.tool == "ToolSearch" or CONDITION.fullmatch(call.tool):
            return Decision()
        if call.tool == "Bash" and (is_brief(call.command) or _canary(call.command)):
            return Decision()
        if call.tool == "Read" and _opening(call, who):
            return Decision()
        if marker_path(call.session).is_file():
            return Decision()
        return Decision(allowed=False, reason=REASON)
