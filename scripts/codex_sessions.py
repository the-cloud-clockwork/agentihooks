"""Names resolve here: Codex skips sessions whose rollout sits outside CODEX_HOME/sessions, as in profile homes."""

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping

CODEX = "codex"
COMMANDS = {"resume": False, "fork": False, "archive": False, "delete": False, "unarchive": True}
VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "-m",
        "--model",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-C",
        "--cd",
        "--enable",
        "--disable",
    }
)
THREAD_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
LIST_ID = 1
TIMEOUT_S = 20


def _session_at(args: list[str]) -> int | None:
    expects_value = False
    for at, arg in enumerate(args):
        if expects_value:
            expects_value = False
        elif arg.startswith("-"):
            expects_value = arg in VALUE_FLAGS
        elif arg in COMMANDS and at + 1 < len(args) and not args[at + 1].startswith("-"):
            return at + 1
        else:
            return None
    return None


def resolve(args: list[str], environ: Mapping[str, str], lookup: Callable | None = None) -> list[str]:
    at = _session_at(args)
    if at is None or THREAD_ID.fullmatch(args[at]):
        return args
    found = (lookup or thread_ids)(args[at], COMMANDS[args[at - 1]], environ)
    return [*args[:at], found[0], *args[at + 1 :]] if len(found) == 1 else args


def _requests(name: str, archived: bool) -> list[dict]:
    return [
        {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "agentihooks", "version": "1"}}},
        {"method": "initialized"},
        {
            "id": LIST_ID,
            "method": "thread/list",
            "params": {
                "searchTerm": name,
                "archived": archived,
                "modelProviders": [],
                "useStateDbOnly": True,
                "limit": 100,
            },
        },
    ]


def _parsed(line: str) -> dict:
    try:
        reply = json.loads(line)
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def thread_ids(name: str, archived: bool, environ: Mapping[str, str], popen: Callable = subprocess.Popen) -> list[str]:
    try:
        server = popen(
            [shutil.which(CODEX) or CODEX, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=dict(environ),
        )
    except OSError:
        return []
    timer = threading.Timer(TIMEOUT_S, server.kill)
    timer.start()
    try:
        server.stdin.write("".join(json.dumps(request) + "\n" for request in _requests(name, archived)))
        server.stdin.flush()
        for line in server.stdout:
            reply = _parsed(line)
            if reply.get("id") == LIST_ID:
                threads = reply.get("result", {}).get("data", [])
                return [thread["id"] for thread in threads if thread.get("name") == name]
        return []
    except OSError:
        return []
    finally:
        timer.cancel()
        server.kill()
        server.wait()
