import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import jwt
import requests

from scripts.github_app import REFUSED, GitAuthRefused, GitHubAppAuth

_AUTH: GitHubAppAuth | None = None
RENEW_SECONDS = 1800
READ_RESERVE = 1000
READ_VERBS = frozenset({"list", "view", "download", "checks", "diff", "status", "watch"})
WRITE_FIELDS = ("-f", "-F", "--field", "--raw-field", "--input")
LOCAL_GROUPS = frozenset({"alias", "auth", "completion", "config", "extension"})


def token_file() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "github-token"


def token() -> str:
    global _AUTH
    mode = os.environ.get("HIVE_GITHUB_AUTH_MODE", "app")
    if mode == "token":
        value = os.environ["GITHUB_TOKEN"]
        if not value:
            raise GitAuthRefused(REFUSED)
        return value
    if mode != "app":
        raise ValueError("GitHub authentication mode must be token or app")
    if "GITHUB_APP_PRIVATE_KEY" not in os.environ:
        value = token_file().read_text().strip()
        if not value:
            raise GitAuthRefused(REFUSED)
        return value
    if _AUTH is None:
        _AUTH = GitHubAppAuth()
    return _AUTH.get_token()


def credential(operation: str, source: TextIO, output: TextIO) -> int:
    if operation != "get":
        return 0
    fields = dict(line.rstrip("\n").split("=", 1) for line in source if "=" in line)
    if fields.get("protocol") == "https" and fields.get("host") == "github.com":
        output.write(f"username=x-access-token\npassword={token()}\n\n")
    return 0


def environment() -> dict[str, str]:
    value = token()
    env = {**os.environ, "GH_TOKEN": value, "GITHUB_TOKEN": value, "AGENTIHOOKS_GITHUB_AUTH_READY": "1"}
    env["GIT_TERMINAL_PROMPT"] = "0"
    index = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, value in enumerate(("", f"!{sys.executable} -P -m scripts.github_app_cli credential")):
        env[f"GIT_CONFIG_KEY_{index + offset}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{index + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 2)
    return env


def renew(last: float, now: float) -> float:
    if os.environ.get("HIVE_GITHUB_AUTH_MODE") != "app" or "GITHUB_APP_PRIVATE_KEY" not in os.environ:
        return last
    if now - last < RENEW_SECONDS:
        return last
    path = token_file()
    staged = path.with_name(f"{path.name}.next")
    try:
        staged.write_text(f"{GitHubAppAuth().get_token()}\n")
        staged.chmod(0o600)
        staged.replace(path)
    except (OSError, GitAuthRefused, jwt.PyJWTError, requests.RequestException):
        return last
    return now


def _method(arguments: list[str]) -> str | None:
    for index, value in enumerate(arguments):
        if value in ("-X", "--method"):
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if value.startswith("--method="):
            return value.removeprefix("--method=")
        if value.startswith("-X"):
            return value.removeprefix("-X")
    return None


def _api_read(arguments: list[str]) -> bool:
    if any(value.lstrip("/") == "rate_limit" for value in arguments):
        return False
    method = _method(arguments)
    if method is not None:
        return method.upper() == "GET"
    if "graphql" in arguments:
        return not any(
            value.split("=", 1)[-1].lstrip().startswith(("mutation", "@")) or value.startswith("--input")
            for value in arguments
        )
    return not any(value.startswith(WRITE_FIELDS) for value in arguments)


def is_read(arguments: list[str]) -> bool:
    if not arguments:
        return False
    if arguments[0] == "api":
        return _api_read(arguments[1:])
    if arguments[0] in LOCAL_GROUPS:
        return False
    return arguments[0] == "search" or (len(arguments) > 1 and arguments[1] in READ_VERBS)


def budget(value: str) -> tuple[int, int, int] | None:
    try:
        reply = requests.get(
            "https://api.github.com/rate_limit",
            headers={"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        headers = reply.headers
        return (
            int(headers["X-RateLimit-Remaining"]),
            int(headers["X-RateLimit-Limit"]),
            int(headers["X-RateLimit-Reset"]),
        )
    except (requests.RequestException, KeyError, ValueError):
        return None


def reserve_refusal(command: list[str], value: str) -> str | None:
    if Path(command[0]).name != "gh" or not is_read(command[1:]):
        return None
    found = budget(value)
    reserve = int(os.environ.get("AGENTIHOOKS_GITHUB_READ_RESERVE") or READ_RESERVE)
    if found is None or found[0] >= reserve:
        return None
    remaining, limit, reset = found
    at = datetime.fromtimestamp(reset, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"GitHub read refused: the shared App limit has {remaining} of {limit} calls left, below the reserve of "
        f"{reserve} kept for launches, queueing and merges; reads resume after the reset at {at}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Use GitHub App installation credentials without saving tokens")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("token", help="Print a current installation token for scripts")
    helper = sub.add_parser("credential", help="Git HTTPS credential helper")
    helper.add_argument("operation", choices=("get", "store", "erase"))
    execute = sub.add_parser("exec", help="Refresh GitHub credentials and execute a process")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == "credential":
            return credential(args.operation, sys.stdin, sys.stdout)
        if args.action == "token":
            print(token())
        else:
            if not args.command:
                parser.error("exec requires a command")
            env = environment()
            refusal = reserve_refusal(args.command, env["GH_TOKEN"])
            if refusal:
                print(refusal, file=sys.stderr)
                return 3
            os.execvpe(args.command[0], args.command, env)
        return 0
    except (OSError, KeyError, ValueError, GitAuthRefused, jwt.PyJWTError, requests.RequestException):
        print(REFUSED, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
