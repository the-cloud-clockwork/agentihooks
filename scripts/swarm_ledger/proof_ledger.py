import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from scripts.swarm_ledger import ledger_link

TIMEOUT = 10


def call(path: str, payload: dict | None, environ) -> dict | None:
    credential = environ.get("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    if not credential:
        sys.exit("AGENTIHOOKS_CONTROLLER_CREDENTIAL is not set")
    request = urllib.request.Request(
        f"{ledger_link.base(environ)}/api/v1/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Controller-Credential": credential},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())["data"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and payload is None:
            return None
        message = json.loads(exc.read())["error"]["message"]
        sys.exit(f"server refused: {exc.code} {message}")


def state(slug: str, environ=os.environ) -> str:
    if call(f"ledgers/{slug}/metadata", None, environ) is None:
        return "missing"
    return "live" if call(f"bin/{slug}", None, environ) is None else "binned"


def create(content: dict, environ=os.environ) -> dict:
    agent, task = environ.get("AGENTIHOOKS_AGENT_NAME"), environ.get("AGENTIHOOKS_SWARM_TASK")
    return call("proof-ledgers", {"agent": agent, "task": task, "content": content}, environ)


def remove(slug: str, environ=os.environ) -> dict:
    return call(f"proof-ledgers/{slug}/remove", {}, environ)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("new").add_argument("--content", required=True)
    sub.add_parser("remove").add_argument("slug")
    args = parser.parse_args(argv)
    if args.action == "new":
        result = create(json.loads(Path(args.content).read_bytes()))
    else:
        result = remove(args.slug)
    print(json.dumps(result))
