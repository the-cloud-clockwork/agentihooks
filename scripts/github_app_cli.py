import argparse
import os
import sys
from pathlib import Path
from typing import TextIO

import jwt
import requests

from scripts.github_app import REFUSED, GitAuthRefused, GitHubAppAuth

_AUTH: GitHubAppAuth | None = None


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
    for offset, value in enumerate(("", f"!{sys.executable} -m scripts.github_app_cli credential")):
        env[f"GIT_CONFIG_KEY_{index + offset}"] = "credential.https://github.com.helper"
        env[f"GIT_CONFIG_VALUE_{index + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 2)
    return env


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
            os.execvpe(args.command[0], args.command, environment())
        return 0
    except (OSError, KeyError, ValueError, GitAuthRefused, jwt.PyJWTError, requests.RequestException):
        print(REFUSED, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
